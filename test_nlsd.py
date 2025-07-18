import os
import shutil
import numpy as np
import argparse
import torch
from torch.utils.data import DataLoader
from dataset import PerturbDataset
from dataset import __classdict__ as DatasetDict, DATASET_TYPE
from models.denoiser import Surrogate, __classdict__ as DenoiserDict
from models.diffuser import SE3Diffuser
from models.loss import se3_err, get_loss
from models.util.constant import BatchedPerturbDatasetOutput
from tqdm import tqdm
import yaml
from models.util import se3
from core.logger import LogTracker, fmt_time
from core.tools import load_checkpoint_model_only
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Tuple, Generator
from core.tools import Timer
from copy import deepcopy

def get_dataloader(test_dataset_argv:Iterable[Dict],
        test_dataloader_argv:Dict, dataset_type:str) -> Tuple[List[str], List[Generator[BatchedPerturbDatasetOutput, None, None]]]:
    name_list = []
    dataloader_list = []
    data_class:DATASET_TYPE = DatasetDict[dataset_type]
    if isinstance(test_dataset_argv, list):
        for dataset_argv in test_dataset_argv:
            name_list.append(dataset_argv['name'])
            base_dataset = data_class(**dataset_argv['base'])
            dataset = PerturbDataset(base_dataset, **dataset_argv['main'])
            if hasattr(dataset, 'collate_fn'):
                test_dataloader_argv['collate_fn'] = getattr(dataset, 'collate_fn')
            dataloader = DataLoader(dataset, **test_dataloader_argv)
            dataloader_list.append(dataloader)
    else:
        assert hasattr(data_class, 'split_dataset'), '{} must has the function \"split_dataset\"'.format(data_class.__class__.__name__)
        root_dataset:DATASET_TYPE = data_class(**test_dataset_argv['base'])
        for base_dataset, name in root_dataset.split_dataset():
            main_args = deepcopy(test_dataset_argv['main'])
            if 'file' in main_args:
                main_args['file'] = main_args['file'].format(name=name)
            dataset = PerturbDataset(base_dataset, **main_args)
            if hasattr(dataset, 'collate_fn'):
                test_dataloader_argv['collate_fn'] = getattr(dataset, 'collate_fn')
            dataloader = DataLoader(dataset, **test_dataloader_argv)
            dataloader_list.append(dataloader)
            name_list.append(name)
    return name_list, dataloader_list


def to_npy(x0:torch.Tensor) -> np.ndarray:
    return x0.detach().cpu().numpy()

@torch.inference_mode()
def test_diffuser(test_loader:DataLoader, name:str, diffuser:SE3Diffuser, logger:logging.Logger, device:torch.device, log_per_iter:int, res_dir:Path):
    diffuser.model.eval()
    logger.info("Test:")
    iterator = tqdm(test_loader, desc=name)
    tracker = LogTracker('Rx','Ry','Rz','tx','ty','tz','R','t','3d3c','5d5c','time')
    cnt = 0
    with iterator:
        N_valid = len(test_loader)
        for i, batch in enumerate(test_loader):
            img = batch['img'].to(device)
            pcd = batch['pcd'].to(device)
            init_extran = batch['extran'].to(device)
            gt_se3 = batch['gt'].to(device)  # transform uncalibrated_pcd to calibrated_pcd
            batch_n = len(gt_se3)
            camera_info = batch['camera_info']
            with Timer() as timer:
                x0_list = diffuser.sampling((img, pcd, init_extran, camera_info), return_intermediate=True)
            dt = timer.elapsed_time
            tracker.update('time', dt, batch_n)
            x0_se3 = x0_list[-1]
            x0_npy_list = [to_npy(se3.log(x0 @ init_extran)) for x0 in x0_list]
            batched_x0_list = np.stack(x0_npy_list, axis=1)  # (B, K, 6)
            for x0 in batched_x0_list:
                np.savetxt(res_dir.joinpath("%06d.txt"%cnt), x0)
                cnt += 1
            R_err, t_err = se3_err(x0_se3, gt_se3)
            R_err = torch.rad2deg(R_err)  # log degree
            if torch.isnan(R_err).sum() + torch.isnan(t_err).sum() > 0:
                logger.warning("nan value detected, skip this batch.")
                N_valid -= 1
                continue
            tracker.update('Rx',torch.mean(R_err[:,0].abs()).item(), batch_n)
            tracker.update('Ry',torch.mean(R_err[:,1].abs()).item(), batch_n)
            tracker.update('Rz',torch.mean(R_err[:,2].abs()).item(), batch_n)
            tracker.update('tx',torch.mean(t_err[:,0].abs()).item(), batch_n)
            tracker.update('ty',torch.mean(t_err[:,1].abs()).item(), batch_n)
            tracker.update('tz',torch.mean(t_err[:,2].abs()).item(), batch_n)
            R_rmse = torch.linalg.norm(R_err, dim=1)
            t_rmse = torch.linalg.norm(t_err, dim=1)
            tracker.update('R',R_rmse.mean().item(), batch_n)
            tracker.update('t',t_rmse.mean().item(), batch_n)
            tracker.update('3d3c', torch.sum(torch.logical_and(R_rmse < 3, t_rmse < 0.03)).item() / batch_n, batch_n)
            tracker.update('5d5c', torch.sum(torch.logical_and(R_rmse < 5, t_rmse < 0.05)).item() / batch_n, batch_n)
            iterator.set_postfix(tracker.result())
            iterator.update(1)
            if (i+1) % log_per_iter == 0:
                logger.info("\tBatch:{}|{}: {}".format(i+1, len(test_loader), tracker.result()))
    assert N_valid > 0, "Fatal Error, no valid batch!"
    return tracker.result(), N_valid / len(test_loader)

def main(config:Dict, model_type:str):
    np.random.seed(config['seed'])
    torch.manual_seed(config['seed'])
    device = config['device']
    surrogate_model:Surrogate = DenoiserDict[config['surrogate']['type']](**config['surrogate']['argv']).to(device)
    dataset_argv = config['dataset']['test']
    dataset_type = config['dataset']['type']
    name_list, dataloader_list = get_dataloader(dataset_argv['dataset'], dataset_argv['dataloader'], dataset_type)
    diffuser = SE3Diffuser(surrogate_model, config['diffuser']['train'], config['diffuser']['val'])
    loss_func = get_loss(config['loss']['type'], **config['loss']['args'])
    diffuser.set_loss(loss_func)
    run_argv = config['run']
    path_argv = config['path']
    experiment_dir = Path(path_argv['base_dir'])
    experiment_dir.mkdir(exist_ok=True, parents=True)
    checkpoints_dir = experiment_dir.joinpath(path_argv['checkpoint'])
    checkpoints_dir.mkdir(exist_ok=True)
    log_dir = experiment_dir.joinpath(path_argv['log'])
    log_dir.mkdir(exist_ok=True)
    # logger
    logger = logging.getLogger(path_argv['log'])
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger_mode = 'w'
    steps = config['diffuser']['val']['n_diff_steps']
    name = "{}_{}_{}".format(model_type, steps, fmt_time())
    res_dir = experiment_dir.joinpath(path_argv['results']).joinpath(name)
    if res_dir.exists():
        shutil.rmtree(str(res_dir))
    res_dir.mkdir(exist_ok=True,parents=True)
    file_handler = logging.FileHandler(str(log_dir) + '/test_{}_{}.log'.format(model_type, steps), mode=logger_mode)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.info('start traing')
    logger.info('args:')
    logger.info(args)
    if path_argv['pretrain'] is not None:
        load_checkpoint_model_only(path_argv['pretrain'], surrogate_model)
        logger.info("Loaded checkpoint from {}".format(path_argv['pretrain']))
    else:
        raise FileNotFoundError("'pretrain' cannot be set to 'None' during test-time")
    ## training
    record_list = []
    for name, dataloader in zip(name_list, dataloader_list):
        surrogate_model.train()
        sub_res_dir = res_dir.joinpath(name)
        sub_res_dir.mkdir()
        record, valid_ratio = test_diffuser(dataloader, name, diffuser, logger, device, run_argv['log_per_iter'], sub_res_dir)
        logger.info("{}: {} | valid: {:.2%}".format(name, record, valid_ratio))
        record_list.append([name, record, valid_ratio])
    logger.info("Summary:")  # view in the bottom
    for name, record, valid_ratio in record_list:
        logger.info("{}: {} | valid: {:.2%}".format(name, record, valid_ratio))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default="experiments/kitti/nlsd/calibnet/log/kitti_nlsd_calibnet.yml", type=str)
    parser.add_argument('--model_type',type=str, default='nlsd')
    args = parser.parse_args()
    config = yaml.load(open(args.config,'r'), yaml.SafeLoader)
    main(config, args.model_type)