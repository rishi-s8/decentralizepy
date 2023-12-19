import logging

import torch
import torch.nn.functional as F
import torchvision
import torchvision.transforms as transforms
from torch import nn
from torch.utils.data import DataLoader

from decentralizepy.datasets.Dataset import Dataset
from decentralizepy.datasets.Partitioner import (
    DataPartitioner,
    DirichletDataPartitioner,
    KShardDataPartitioner,
    SimpleDataPartitioner,
)
from decentralizepy.mappings.Mapping import Mapping
from decentralizepy.models.Model import Model

NUM_CLASSES = 10


class CIFAR10(Dataset):
    """
    Class for the FEMNIST dataset

    """

    def load_trainset(self):
        """
        Loads the training set. Partitions it if needed.

        """
        logging.info("Loading training set.")
        trainset = torchvision.datasets.CIFAR10(
            root=self.train_dir, train=True, download=True, transform=self.transform
        )

        if self.__validating__ and self.validation_source == "Train":
            logging.info("Extracting the validation set from the train set.")
            self.validationset, trainset = torch.utils.data.random_split(
                trainset,
                [self.validation_size, 1 - self.validation_size],
                torch.Generator().manual_seed(self.random_seed),
            )

        self.c_len = len(trainset)

        if self.sizes == None:  # Equal distribution of data among processes
            e = self.c_len // self.num_partitions
            frac = e / self.c_len
            self.sizes = [frac] * self.num_partitions
            self.sizes[-1] += 1.0 - frac * self.num_partitions
            logging.debug("Size fractions: {}".format(self.sizes))

        if not self.partition_niid or self.partition_niid == "iid":
            # IID partitioning
            self.training_partitions = DataPartitioner(
                trainset, sizes=self.sizes, seed=self.random_seed
            )
        elif self.partition_niid == "simple":
            self.training_partitions = SimpleDataPartitioner(
                trainset, sizes=self.sizes, seed=self.random_seed
            )
        elif self.partition_niid == "dirichlet":
            self.training_partitions = DirichletDataPartitioner(
                trainset,
                sizes=self.sizes,
                seed=self.random_seed,
                alpha=self.alpha,
                num_classes=self.num_classes,
            )
        elif (
            self.partition_niid == "kshard" or str(self.partition_niid) == "True"
        ):  # Backward compatibility
            if str(self.partition_niid) == "True":
                logging.warn(
                    "Using True as partition_niid is deprecated. Use kshard instead. Will be removed in future versions."
                )
            train_data = {key: [] for key in range(self.num_classes)}
            for x, y in trainset:
                train_data[y].append(x)
            all_trainset = []
            for y, x in train_data.items():
                all_trainset.extend([(a, y) for a in x])
            self.training_partitions = KShardDataPartitioner(
                all_trainset, self.sizes, shards=self.shards, seed=self.random_seed
            )
        else:
            raise NotImplementedError(
                "Partitioning method {} not implemented".format(self.partition_niid)
            )
        self.trainset = self.training_partitions.use(self.dataset_id)

    def load_testset(self):
        """
        Loads the testing set.

        """
        logging.info("Loading testing set.")

        self.testset = torchvision.datasets.CIFAR10(
            root=self.test_dir, train=False, download=True, transform=self.transform
        )

        if self.__validating__ and self.validation_source == "Test":
            logging.info("Extracting the validation set from the test set.")
            self.validationset, self.testset = torch.utils.data.random_split(
                self.testset,
                [self.validation_size, 1 - self.validation_size],
                torch.Generator().manual_seed(self.random_seed),
            )

    def __init__(
        self,
        rank: int,
        machine_id: int,
        mapping: Mapping,
        random_seed: int = 1234,
        only_local=False,
        train_dir="",
        test_dir="",
        sizes="",
        test_batch_size=1024,
        partition_niid="simple",
        alpha=100,
        shards=1,
        validation_source="",
        validation_size="",
        *args,
        **kwargs
    ):
        """
        Constructor which reads the data files, instantiates and partitions the dataset

        Parameters
        ----------
        rank : int
            Rank of the current process (to get the partition).
        machine_id : int
            Machine ID
        mapping : decentralizepy.mappings.Mapping
            Mapping to convert rank, machine_id -> uid for data partitioning
            It also provides the total number of global processes
        random_seed : int, optional
            Random seed for the dataset
        only_local : bool, optional
            True if the dataset needs to be partioned only among local procs, False otherwise
        train_dir : str, optional
            Path to the training data files. Required to instantiate the training set
            The training set is partitioned according to the number of global processes and sizes
        test_dir : str. optional
            Path to the testing data files Required to instantiate the testing set
        sizes : list(int), optional
            A list of fractions specifying how much data to alot each process. Sum of fractions should be 1.0
            By default, each process gets an equal amount.
        test_batch_size : int, optional
            Batch size during testing. Default value is 64
        partition_niid: string, optional
            One of 'simple', 'kshard', 'dirichlet'
        alpha: float, optional
            Parameter for Dirichlet Partitioner
        shards: int, optional
            Number of shards for KShard Partitioner
        validation_source: string, optional
            Source of validation set. One of 'Test', 'Train'
        validation_size: int, optional
            Fraction of the Test or Train set used as validation set
        """
        super().__init__(
            rank,
            machine_id,
            mapping,
            random_seed,
            only_local,
            train_dir,
            test_dir,
            sizes,
            test_batch_size,
            validation_source,
            validation_size,
            *args,
            **kwargs
        )

        self.num_classes = NUM_CLASSES

        self.partition_niid = partition_niid
        self.alpha = alpha
        self.shards = shards
        self.transform = transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
            ]
        )

        if self.__training__:
            self.load_trainset()

        if self.__testing__:
            self.load_testset()

    def get_trainset(self, batch_size=1, shuffle=False, dataset_id=None):
        """
        Function to get the training set

        Parameters
        ----------
        batch_size : int, optional
            Batch size for learning

        Returns
        -------
        torch.utils.Dataset(decentralizepy.datasets.Data)

        Raises
        ------
        RuntimeError
            If the training set was not initialized

        """
        if self.__training__:
            if dataset_id is not None:
                return DataLoader(
                    self.training_partitions.use(dataset_id), # This is costly for lists, fix it.
                    batch_size=batch_size,
                    shuffle=shuffle,
                )
            elif self.trainset is not None:
                return DataLoader(self.trainset, batch_size=batch_size, shuffle=shuffle)
            else:
                raise RuntimeError("Training set not initialized!")
        raise RuntimeError("Training set not initialized!")

    def get_testset(self):
        """
        Function to get the test set

        Returns
        -------
        torch.utils.Dataset(decentralizepy.datasets.Data)

        Raises
        ------
        RuntimeError
            If the test set was not initialized

        """
        if self.__testing__:
            return DataLoader(self.testset, batch_size=self.test_batch_size)
        raise RuntimeError("Test set not initialized!")

    def get_validationset(self):
        """
        Function to get the validation set

        Returns
        -------
        torch.utils.Dataset(decentralizepy.datasets.Data)

        Raises
        ------
        RuntimeError
            If the test set was not initialized

        """
        if self.__validating__:
            return DataLoader(self.validationset, batch_size=self.test_batch_size)
        raise RuntimeError("Validation set not initialized!")

    def test(self, model, loss):
        """
        Function to evaluate model on the test dataset.

        Parameters
        ----------
        model : decentralizepy.models.Model
            Model to evaluate
        loss : torch.nn.loss
            Loss function to use

        Returns
        -------
        tuple(float, float)

        """
        model.eval()
        if torch.cuda.is_available():
            model = model.cuda()
        testloader = self.get_testset()

        logging.debug("Test Loader instantiated.")

        class_correct = torch.zeros(NUM_CLASSES, device = self.device, dtype = torch.int32)
        class_total = torch.zeros(NUM_CLASSES, device = self.device, dtype=torch.int32)

        with torch.no_grad():
            loss_val = 0.0
            count = 0
            for elems, labels in testloader:
                if torch.cuda.is_available():
                    elems = elems.cuda()
                    labels = labels.cuda()
                outputs = model(elems)
                loss_val += loss(outputs, labels) * labels.size(0)
                outputs = outputs
                labels = labels
                count += labels.size(0)
                _, predictions = torch.max(outputs, 1)

                correct = predictions.eq(labels)

                for i in range(NUM_CLASSES):
                    class_correct[i] += correct[labels == i].sum()
                    class_total[i] += (labels == i).sum()

        logging.debug("Predicted on the test set")

        overall_accuracy = 100 * class_correct.sum().float() / class_total.sum() if class_total.sum() != 0 else 100.0
        loss_val = loss_val / count

        per_class_accuracy = 100 * class_correct.float() / class_total.where(class_total != 0, torch.tensor(1.0))


        for i in range(NUM_CLASSES):
            logging.debug("Accuracy for class {} is: {:.1f} %".format(i, per_class_accuracy[i]))

        logging.info("Overall test accuracy is: {:.1f} %".format(overall_accuracy))
        model = model.cpu()
        return overall_accuracy.item(), loss_val.item()

    def validate(self, model, loss):
        """
        Function to evaluate model on the validation dataset.

        Parameters
        ----------
        model : decentralizepy.models.Model
            Model to evaluate
        loss : torch.nn.loss
            Loss function to use

        Returns
        -------
        tuple(float, float)

        """
        model.eval()
        validationloader = self.get_validationset()

        logging.debug("Validation Loader instantiated.")

        correct_pred = [0 for _ in range(NUM_CLASSES)]
        total_pred = [0 for _ in range(NUM_CLASSES)]

        total_correct = 0
        total_predicted = 0

        with torch.no_grad():
            loss_val = 0.0
            count = 0
            for elems, labels in validationloader:
                outputs = model(elems)
                loss_val += loss(outputs, labels).item()
                count += 1
                _, predictions = torch.max(outputs, 1)
                for label, prediction in zip(labels, predictions):
                    logging.debug("{} predicted as {}".format(label, prediction))
                    if label == prediction:
                        correct_pred[label] += 1
                        total_correct += 1
                    total_pred[label] += 1
                    total_predicted += 1

        logging.debug("Predicted on the validation set")

        for key, value in enumerate(correct_pred):
            if total_pred[key] != 0:
                accuracy = 100 * float(value) / total_pred[key]
            else:
                accuracy = 100.0
            logging.debug("Accuracy for class {} is: {:.1f} %".format(key, accuracy))

        accuracy = 100 * float(total_correct) / total_predicted
        loss_val = loss_val / count
        logging.info("Overall validation accuracy is: {:.1f} %".format(accuracy))
        return accuracy, loss_val


class CNN(Model):
    """
    Class for a CNN Model for CIFAR10

    """

    def __init__(self):
        """
        Constructor. Instantiates the CNN Model
            with 10 output classes

        """
        super().__init__()
        # 1.6 million params
        self.conv1 = nn.Conv2d(3, 6, 5)
        self.pool = nn.MaxPool2d(2, 2)
        self.conv2 = nn.Conv2d(6, 16, 5)
        self.fc1 = nn.Linear(16 * 5 * 5, 120)
        self.fc2 = nn.Linear(120, 84)
        self.fc3 = nn.Linear(84, NUM_CLASSES)

    def forward(self, x):
        """
        Forward pass of the model

        Parameters
        ----------
        x : torch.tensor
            The input torch tensor

        Returns
        -------
        torch.tensor
            The output torch tensor

        """
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = torch.flatten(x, 1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x


class LeNet(Model):
    """
    Class for a LeNet Model for CIFAR10
    Inspired by original LeNet network for MNIST: https://ieeexplore.ieee.org/abstract/document/726791

    """

    def __init__(self):
        """
        Constructor. Instantiates the CNN Model
            with 10 output classes

        """
        super().__init__()
        self.conv1 = nn.Conv2d(3, 32, 5, padding="same")
        self.pool = nn.MaxPool2d(2, 2)
        self.gn1 = nn.GroupNorm(2, 32)
        self.conv2 = nn.Conv2d(32, 32, 5, padding="same")
        self.gn2 = nn.GroupNorm(2, 32)
        self.conv3 = nn.Conv2d(32, 64, 5, padding="same")
        self.gn3 = nn.GroupNorm(2, 64)
        self.fc1 = nn.Linear(64 * 4 * 4, NUM_CLASSES)

    def forward(self, x):
        """
        Forward pass of the model

        Parameters
        ----------
        x : torch.tensor
            The input torch tensor

        Returns
        -------
        torch.tensor
            The output torch tensor

        """
        x = self.pool(F.relu(self.gn1(self.conv1(x))))
        x = self.pool(F.relu(self.gn2(self.conv2(x))))
        x = self.pool(F.relu(self.gn3(self.conv3(x))))
        x = torch.flatten(x, 1)
        x = self.fc1(x)
        return x


# Taken from: https://github.com/gong-xuan/FedKD/blob/master/models/resnet8.py
class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(
            in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False
        )
        # self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(
            planes, planes, kernel_size=3, stride=1, padding=1, bias=False
        )
        # self.bn2 = nn.BatchNorm2d(planes)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != self.expansion * planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(
                    in_planes,
                    self.expansion * planes,
                    kernel_size=1,
                    stride=stride,
                    bias=False,
                ),
                # nn.BatchNorm2d(self.expansion * planes),
            )

    # def forward(self, x):
    #     out = F.relu(self.bn1(self.conv1(x)))
    #     out = self.bn2(self.conv2(out))
    #     out += self.shortcut(x)
    #     out = F.relu(out)
    #     return out
    def forward(self, x):
        out = F.relu(self.conv1(x))
        out = self.conv2(out)
        out += self.shortcut(x)
        out = F.relu(out)
        return out

class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, in_planes, planes, stride=1):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=1, bias=False)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3,
                               stride=stride, padding=1, bias=False)
        self.conv3 = nn.Conv2d(planes, self.expansion *
                               planes, kernel_size=1, bias=False)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != self.expansion*planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion*planes,
                          kernel_size=1, stride=stride, bias=False),
            )

    def forward(self, x):
        out = F.relu(self.conv1(x))
        out = F.relu(self.conv2(out))
        out = self.conv3(out)
        out += self.shortcut(x)
        out = F.relu(out)
        return out
    
class ResNet(nn.Module):
    def __init__(self, block, num_blocks, num_classes):
        super(ResNet, self).__init__()
        self.in_planes = 64
        self.pool_size = 8

        self.conv1 = nn.Conv2d(3, 64, kernel_size=7,
                               stride=2, padding=3, bias=False)
        self.layer1 = self._make_layer(block, 64, num_blocks[0], stride=1)
        self.layer2 = self._make_layer(block, 128, num_blocks[1], stride=2)
        self.layer3 = self._make_layer(block, 256, num_blocks[2], stride=2)
        self.layer4 = self._make_layer(block, 512, num_blocks[3], stride=2)
        self.pool = nn.AdaptiveAvgPool2d((self.pool_size,self.pool_size)) 
        self.linear = nn.Linear(512*block.expansion*self.pool_size**2, num_classes, bias=False)

        self.skip_idx = -1

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1]*(num_blocks-1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def forward(self, x):
        out = F.relu(self.conv1(x))
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = self.layer4(out)
        out = self.pool(out)
        out = out.view(out.size(0), -1)
        out = self.linear(out)
        return out

def ResNet18():
    return ResNet(BasicBlock, [2, 2, 2, 2], NUM_CLASSES)


class ResNet8(Model):
    def __init__(self, num_classes=10):
        super(ResNet8, self).__init__()
        block = BasicBlock
        num_blocks = [1, 1, 1]
        self.num_classes = num_classes
        self.in_planes = 128

        self.conv1 = nn.Conv2d(3, 128, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(128)
        self.layer1 = self._make_layer(block, 128, num_blocks[0], stride=1)
        self.layer2 = self._make_layer(block, 256, num_blocks[1], stride=2)
        self.layer3 = self._make_layer(block, 512, num_blocks[2], stride=2)
        self.linear1 = nn.Linear(2048, num_classes)
        self.linear2 = nn.Linear(2048, num_classes)
        self.emb = nn.Embedding(num_classes, num_classes)
        self.emb.weight = nn.Parameter(torch.eye(num_classes))

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.layer1(out)  # b*128*32*32
        out = self.layer2(out)  # b*256*16*16
        out = self.layer3(out)  # b*512*8*8
        self.inner = out
        out = F.avg_pool2d(out, 4)
        out = out.view(out.size(0), -1)

        self.flatten_feat = out  # b*2048
        out = self.linear1(out)
        return out

    def get_attentions(self):
        inner_copy = self.inner.detach().clone()  # b*512*8*8
        inner_copy.requires_grad = True
        out = F.avg_pool2d(inner_copy, 4)  # b*512*2*2
        out = out.view(out.size(0), -1)  # b*2048
        out = self.linear1(out)  # b*num_classes
        losses = out.sum(dim=0)  # num_classes
        cams = []
        # import ipdb;ipdb.set_trace()
        # assert losses.shape ==self.num_classes
        for n in range(self.num_classes):
            loss = losses[n]
            self.zero_grad()
            if n < self.num_classes - 1:
                loss.backward(retain_graph=True)
            else:
                loss.backward()
            grads_val = inner_copy.grad
            weights = grads_val.mean(dim=(2, 3), keepdim=True)  # b*512*1*1
            cams.append(F.relu((weights.detach() * self.inner).sum(dim=1)))  # b*8*8
        atts = torch.stack(cams, dim=1)
        return atts
