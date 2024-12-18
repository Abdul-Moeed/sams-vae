from typing import Literal, Optional, Sequence

import anndata
import numpy as np
import pandas as pd
import torch
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader

from sams_vae.analysis.average_treatment_effects import (
    estimate_data_average_treatment_effects,
)
from sams_vae.data.utils.perturbation_datamodule import (
    ObservationNormalizationStatistics,
    PerturbationDataModule,
)
from sams_vae.data.utils.perturbation_dataset import SCRNASeqTensorPerturbationDataset


class VeltenDataModule(PerturbationDataModule):
    def __init__(
            self,
            batch_size: int = 128,
            data_path: Optional[str] = None,
    ):
        super().__init__()
        self.batch_size = batch_size
        # self.adata = anndata.read_h5ad(data_path)
        self.adata = data_path

        """
        # HVG selection
        n_genes=3000
        adata.raw = self.adata.copy()
        sc.pp.normalize_total(self.adata, target_sum=1e4)
        sc.pp.log1p(self.adata)

        sc.pp.highly_variable_genes(
            self.adata, min_mean=0.0125, max_mean=3, min_disp=0.5, n_top_genes=n_genes
        )
        variable_genes = self.adata.var.highly_variable
        self.adata.X = adata.raw.X
        self.adata = self.adata[:, variable_genes]
        """

        # Metadata processing
        self.adata.obs.loc[self.adata.obs['patient'] == 'Reference', 'status'] = 'healthy'
        self.adata.obs.loc[self.adata.obs['patient'] == 'A.0', 'status'] = 'healthy'
        self.adata = self.adata[(self.adata.obs.status == 'healthy') | (self.adata.obs.status == 'leukemic')]

        # Add batch column
        self.adata.obs['dataset'] = 'velten'
        self.adata.obs.loc[self.adata.obs.patient == 'Reference', 'dataset'] = 'triana'
        remove_ct = ["other", "NA", "T cell", "B cell"]
        mask = ~self.adata.obs['ct'].str.contains('|'.join(remove_ct))
        self.adata = self.adata[mask].copy()

        min_instances = 500
        keep_ct = self.adata.obs.groupby("ct").filter(lambda x: len(x) > min_instances).ct.unique().tolist()
        self.adata = self.adata[self.adata.obs.ct.isin(keep_ct)].copy()

        holdout_cells = ['B.3', 'A.2']
        self.adata.obs['is_holdout'] = False
        self.adata.obs.loc[self.adata.obs['patient'].isin(holdout_cells), 'is_holdout'] = True

        self.adata.obs['domain'] = self.adata.obs['patient'].astype(str)
        self.adata.obs.loc[self.adata.obs['status'] == 'healthy', 'domain'] = 'healthy'
        self.adata.obs['index'] = range(self.adata.shape[0])

        #self.adata_train = self.adata[~self.adata.obs['is_holdout']]
        #self.adata_holdout = self.adata[self.adata.obs['is_holdout']]
        self.adata_train = self.adata

        # define splits
        idx = np.arange(self.adata_train.shape[0])
        train_idx, test_idx = train_test_split(idx, train_size=0.8, random_state=0)
        train_idx, val_idx = train_test_split(train_idx, train_size=0.8, random_state=0)

        self.adata_train.obs["split"] = None
        self.adata_train.obs.iloc[
            train_idx, self.adata_train.obs.columns.get_loc("split")
        ] = "train"
        self.adata_train.obs.iloc[val_idx, self.adata_train.obs.columns.get_loc("split")] = "val"
        self.adata_train.obs.iloc[test_idx, self.adata_train.obs.columns.get_loc("split")] = "test"

        # encode dosages
        # combine non-targeting guides to single label
        self.adata_train.obs["T"] = self.adata_train.obs["domain"]
        dosage_df = pd.get_dummies(self.adata_train.obs["T"])
        # encode non-targeting guides as 0
        dosage_df = dosage_df.drop(columns=["healthy"])

        self.d_var_info = dosage_df.T[[]]
        D = torch.from_numpy(dosage_df.to_numpy().astype(np.float32))

        X = torch.from_numpy(self.adata_train.X.todense().astype(np.float32))

        ids_tr = self.adata_train.obs[self.adata_train.obs["split"] == "train"].index
        X_tr = X[(self.adata_train.obs["split"] == "train").to_numpy()]
        D_tr = D[(self.adata_train.obs["split"] == "train").to_numpy()]

        ids_val = self.adata_train.obs[self.adata_train.obs["split"] == "val"].index
        X_val = X[(self.adata_train.obs["split"] == "val").to_numpy()]
        D_val = D[(self.adata_train.obs["split"] == "val").to_numpy()]

        ids_test = self.adata_train.obs[self.adata_train.obs["split"] == "test"].index
        X_test = X[(self.adata_train.obs["split"] == "test").to_numpy()]
        D_test = D[(self.adata_train.obs["split"] == "test").to_numpy()]

        self.train_dataset = SCRNASeqTensorPerturbationDataset(
            X=X_tr, D=D_tr, ids=ids_tr
        )
        self.val_dataset = SCRNASeqTensorPerturbationDataset(
            X=X_val, D=D_val, ids=ids_val
        )
        self.test_dataset = SCRNASeqTensorPerturbationDataset(
            X=X_test, D=D_test, ids=ids_test
        )

        x_tr_mean = X_tr.mean(0)
        x_tr_std = X_tr.std(0)
        log_x_tr = torch.log(X_tr + 1)
        log_x_tr_mean = log_x_tr.mean(0)
        log_x_tr_std = log_x_tr.std(0)

        epsilon = 1e-4
        self.x_train_statistics = ObservationNormalizationStatistics(
            x_mean=x_tr_mean,
            x_std=x_tr_std + epsilon,
            log_x_mean=log_x_tr_mean,
            log_x_std=log_x_tr_std + epsilon,
        )

        # because there are no perturbation combinations in this simulation,
        # unique_perturbations are the same as the observed perturbations
        # generate unique intervention info dataframe
        df = self.adata_train.obs.groupby("T")["split"].agg(set).reset_index()
        for split in ["train", "val", "test"]:
            df[split] = df["split"].apply(lambda x: split in x)
        df = df.set_index("T").drop(columns=["split"])
        self.unique_observed_intervention_df = df

        # generate mapping from intervention names to dosages
        self.adata_train.obs["i"] = np.arange(self.adata_train.shape[0])
        idx_map = self.adata_train.obs.drop_duplicates("T").set_index("T")["i"].to_dict()
        self.unique_intervention_dosage_map = {k: D[v] for k, v in idx_map.items()}

    def train_dataloader(self):
        return DataLoader(self.train_dataset, batch_size=self.batch_size, shuffle=True)

    def val_dataloader(self):
        return DataLoader(self.val_dataset, batch_size=self.batch_size, shuffle=False)

    def test_dataloader(self):
        return DataLoader(self.test_dataset, batch_size=self.batch_size, shuffle=False)

    def get_train_perturbation_obs_counts(self) -> torch.Tensor:
        return self.train_dataset.get_dosage_obs_per_dim()

    def get_val_perturbation_obs_counts(self) -> torch.Tensor:
        #
        return self.val_dataset.get_dosage_obs_per_dim()

    def get_test_perturbation_obs_counts(self) -> torch.Tensor:
        return self.test_dataset.get_dosage_obs_per_dim()

    def get_x_var_info(self) -> pd.DataFrame:
        return self.adata.var.copy()

    def get_d_var_info(self) -> pd.DataFrame:
        return self.d_var_info.copy()

    def get_obs_info(self) -> pd.DataFrame:
        return self.adata.obs.copy()

    def get_x_train_statistics(self) -> ObservationNormalizationStatistics:
        return self.x_train_statistics

    def get_unique_observed_intervention_info(self) -> pd.DataFrame:
        return self.unique_observed_intervention_df.copy()

    def get_unique_observed_intervention_dosages(
            self, pert_names: Sequence
    ) -> torch.Tensor:
        D = torch.zeros((len(pert_names), self.d_var_info.shape[0]))
        for i, pert_name in enumerate(pert_names):
            D[i] = self.unique_intervention_dosage_map[pert_name]
        return D

    def get_estimated_average_treatment_effects(
            self,
            method: Literal["mean", "perturbseq"],
            split: Optional[str] = None,
    ) -> Optional[anndata.AnnData]:
        adata = self.adata
        if split is not None:
            adata = adata[adata.obs["split"] == split]
        return estimate_data_average_treatment_effects(
            adata,
            label_col="T",
            control_label="non-targeting",
            method=method,
        )

    def get_simulated_latent_effects(self) -> Optional[anndata.AnnData]:
        return None
