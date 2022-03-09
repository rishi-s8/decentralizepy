import json
import logging
import os
from pathlib import Path

import numpy as np
import torch

from decentralizepy.sharing.Sharing import Sharing


class TopK(Sharing):
    """
    This class implements topk selection of model parameters based on the model change since the beginning of the
    communication step: --> Use ModelChangeAccumulator

    """

    def __init__(
        self,
        rank,
        machine_id,
        communication,
        mapping,
        graph,
        model,
        dataset,
        log_dir,
        alpha=1.0,
        dict_ordered=True,
        save_shared=False,
        metadata_cap=1.0,
        accumulation=False,
    ):
        """
        Constructor

        Parameters
        ----------
        rank : int
            Local rank
        machine_id : int
            Global machine id
        communication : decentralizepy.communication.Communication
            Communication module used to send and receive messages
        mapping : decentralizepy.mappings.Mapping
            Mapping (rank, machine_id) -> uid
        graph : decentralizepy.graphs.Graph
            Graph reprensenting neighbors
        model : decentralizepy.models.Model
            Model to train
        dataset : decentralizepy.datasets.Dataset
            Dataset for sharing data. Not implemented yet! TODO
        log_dir : str
            Location to write shared_params (only writing for 2 procs per machine)
        alpha : float
            Percentage of model to share
        dict_ordered : bool
            Specifies if the python dict maintains the order of insertion
        save_shared : bool
            Specifies if the indices of shared parameters should be logged
        metadata_cap : float
            Share full model when self.alpha > metadata_cap

        """
        super().__init__(
            rank, machine_id, communication, mapping, graph, model, dataset, log_dir
        )
        self.alpha = alpha
        self.dict_ordered = dict_ordered
        self.save_shared = save_shared
        self.metadata_cap = metadata_cap
        self.total_meta = 0
        self.accumulation = accumulation

        if self.save_shared:
            # Only save for 2 procs: Save space
            if rank != 0 or rank != 1:
                self.save_shared = False

        if self.save_shared:
            self.folder_path = os.path.join(
                self.log_dir, "shared_params/{}".format(self.rank)
            )
            Path(self.folder_path).mkdir(parents=True, exist_ok=True)

    def extract_top_gradients(self):
        """
        Extract the indices and values of the topK gradients.
        The gradients must have been accumulationd.

        Returns
        -------
        tuple
            (a,b). a: The magnitudes of the topK gradients, b: Their indices.

        """
        tensors_to_cat = [v.data.flatten() for _, v in self.model.state_dict().items()]
        concated = torch.cat(tensors_to_cat, dim=0)
        if self.accumulation:
            logging.info(
                "TopK extract gradients based on accumulated model parameter change"
            )
            diff = self.model.prev_model_params + (concated - self.model.prev)
        else:
            diff = concated - self.model.prev_model_params
        G_topk = torch.abs(diff)

        std, mean = torch.std_mean(G_topk, unbiased=False)
        self.std = std.item()
        self.mean = mean.item()
        value, ind = torch.topk(
            G_topk, round(self.alpha * G_topk.shape[0]), dim=0, sorted=False
        )

        # only needed when ModelChangeAccumulator.accumulation = True
        # does not cause problems otherwise
        if self.accumulation:
            self.model.prev_model_params[ind] = 0.0  # torch.zeros((len(G_topk),))
        return value, ind

    def serialized_model(self):
        """
        Convert model to a dict. self.alpha specifies the fraction of model to send.

        Returns
        -------
        dict
            Model converted to a dict

        """
        if self.alpha > self.metadata_cap:  # Share fully
            return super().serialized_model()

        with torch.no_grad():
            _, G_topk = self.extract_top_gradients()

            if self.save_shared:
                shared_params = dict()
                shared_params["order"] = list(self.model.state_dict().keys())
                shapes = dict()
                for k, v in self.model.state_dict().items():
                    shapes[k] = list(v.shape)
                shared_params["shapes"] = shapes

                shared_params[self.communication_round] = G_topk.tolist()

                with open(
                    os.path.join(
                        self.folder_path,
                        "{}_shared_params.json".format(self.communication_round + 1),
                    ),
                    "w",
                ) as of:
                    json.dump(shared_params, of)

            logging.info("Extracting topk params")

            tensors_to_cat = [v.data.flatten() for v in self.model.parameters()]
            T = torch.cat(tensors_to_cat, dim=0)
            T_topk = T[G_topk]

            logging.info("Generating dictionary to send")

            m = dict()

            if not self.dict_ordered:
                raise NotImplementedError

            m["indices"] = G_topk.numpy().astype(np.int32)
            m["params"] = T_topk.numpy()

            assert len(m["indices"]) == len(m["params"])
            logging.info("Elements sending: {}".format(len(m["indices"])))

            logging.info("Generated dictionary to send")

            logging.info("Converted dictionary to pickle")
            self.total_data += len(self.communication.encrypt(m["params"]))
            self.total_meta += len(self.communication.encrypt(m["indices"]))

            return m

    def deserialized_model(self, m):
        """
        Convert received dict to state_dict.

        Parameters
        ----------
        m : dict
            dict received

        Returns
        -------
        state_dict
            state_dict of received

        """
        if self.alpha > self.metadata_cap:  # Share fully
            return super().deserialized_model(m)

        with torch.no_grad():
            state_dict = self.model.state_dict()

            if not self.dict_ordered:
                raise NotImplementedError

            shapes = []
            lens = []
            tensors_to_cat = []
            for _, v in state_dict.items():
                shapes.append(v.shape)
                t = v.flatten()
                lens.append(t.shape[0])
                tensors_to_cat.append(t)

            T = torch.cat(tensors_to_cat, dim=0)
            index_tensor = torch.tensor(m["indices"], dtype=torch.long)
            logging.debug("Original tensor: {}".format(T[index_tensor]))
            T[index_tensor] = torch.tensor(m["params"])
            logging.debug("Final tensor: {}".format(T[index_tensor]))
            start_index = 0
            for i, key in enumerate(state_dict):
                end_index = start_index + lens[i]
                state_dict[key] = T[start_index:end_index].reshape(shapes[i])
                start_index = end_index

            return state_dict
