import pdb
import os
import sys
sys.path.append(os.path.realpath('.'))
sys.path.append('../bidireaction-trajectory-prediction/')
sys.path.append('../bidireaction-trajectory-prediction/datasets')
import torch
from torch import nn, optim
from torch.nn import functional as F

import pickle as pkl
from datasets import make_dataloader
from bitrap.modeling import make_model
from bitrap.utils.dataset_utils import restore
from bitrap.engine.utils import print_info, post_process


from utils.datasets_utils import Experiment_Parameters, setup_loo_experiment, traj_dataset_bitrap
import logging
# Local constants
from utils.constants import OBS_TRAJ_VEL, OBS_TRAJ_ACC, OBS_NEIGHBORS, PRED_TRAJ_VEL, OBS_TRAJ, PRED_TRAJ, TRAINING_CKPT_DIR, REFERENCE_IMG

import argparse
from configs import cfg
from termcolor import colored
import numpy as np
import tqdm
import pdb
import matplotlib.pyplot as plt
import random

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="PyTorch Object Detection Training")
    parser.add_argument('--gpu', default='0', type=str)
    parser.add_argument('--id-test',
                        type=int, default=2, metavar='N',
                        help='id of the dataset to use as test in LOO (default: 2)')
    parser.add_argument('--batch-size', '--b',
                        type=int, default=64, metavar='N',
                        help='input batch size for training (default: 256)')
    parser.add_argument(
        "--config_file",
        default="../bidireaction-trajectory-prediction/configs/bitrap_np_ETH.yml",
        metavar="FILE",
        help="path to config file",
        type=str,
    )
    parser.add_argument(
        "opts",
        help="Modify config options using the command-line",
        default=None,
        nargs=argparse.REMAINDER,
    )
    parser.add_argument('--log-level',type=int, default=20,help='Log level (default: 20)')
    parser.add_argument('--log-file',default='',help='Log file (default: standard output)')
    args = parser.parse_args()
    # Loggin format
    logging.basicConfig(format='%(levelname)s: %(message)s',level=args.log_level)

    # Load the default parameters
    experiment_parameters = Experiment_Parameters()

    #### Data: our way
    dataset_dir   = "datasets/"
    dataset_names = ['eth-hotel','eth-univ','ucy-zara01','ucy-zara02','ucy-univ']
    model_name = 'model_deterministic'

    #### Data: BitTrap way
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    cfg.BATCH_SIZE = 1
    cfg.TEST.BATCH_SIZE = 1
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    # get dataloaders
    test_dataloader = make_dataloader(cfg, 'test')
    env             = test_dataloader.dataset.dataset.env

    # Load the ensemble models
    nsamples = 100
    ensemble = []
    for i in range(5):
        cfg.CKPT_DIR = 'training_checkpoints/bitrap-zara01-0{}.pth'.format(i+1)
        # build model, optimizer and scheduler
        model = make_model(cfg)
        model = model.to(cfg.DEVICE)
        if os.path.isfile(cfg.CKPT_DIR):
            model.load_state_dict(torch.load(cfg.CKPT_DIR))
            print(colored('Loaded checkpoint:{}'.format(cfg.CKPT_DIR), 'blue', 'on_green'))
        else:
            print(colored('The cfg.CKPT_DIR id not a file: {}'.format(cfg.CKPT_DIR), 'green', 'on_red'))
        model.K = 1
        ensemble.append(model)

    ##################################################################
    # With BiTrap data (to show how to use normalization)
    with torch.set_grad_enabled(False):
        for iters, batch in enumerate(test_dataloader, start=1):
            # Input: n_batchesx8x6 (un-normalized)
            X_global     = batch['input_x'].to(cfg.DEVICE)
            # This is the GT: n_batchesx12x2 (un-normalized)
            y_global     = batch['target_y']

            # Input: n_batchesx8x6 (**normalized**)
            node_type = env.NodeType[0]
            state     = {'position':['x','y'], 'velocity':['x','y'], 'acceleration':['x','y']}
            _, std    = env.get_standardize_params(state, node_type)
            std[0:2]  = env.attention_radius[(node_type, node_type)]
            # Reference point: the last observed positio, batch_size x 6
            rel_state        = np.zeros_like(batch['input_x'][:,0])
            rel_state[:,0:2] = np.array(batch['input_x'])[:,-1, 0:2]
            rel_state        = np.expand_dims(rel_state,axis=1)
            my_x_st          = env.standardize(batch['input_x'],state,node_type,mean=rel_state,std=std)
            my_input_x       = torch.tensor(my_x_st,dtype=torch.float).to(cfg.DEVICE)

            # Dictionary with keys pairs node_type x node_type
            neighbors_un    = restore(batch['neighbors_x'])
            my_neighbors_st = {}
            my_neighbors_st[(node_type,node_type)] = [[]]
            for n in neighbors_un[(node_type,node_type)][0]:
                n_st = env.standardize(n, state, node_type, mean=rel_state, std=std)
                my_neighbors_st[(node_type,node_type)][0].append(n_st)
            n_neighbors = len(neighbors_un[(node_type,node_type)][0])

            # Should be a list of x tensors 8x6
            # Dictionary with keys pairs node_type x node_type
            my_adjacency = {}
            my_adjacency[(node_type,node_type)] = [torch.tensor(np.ones(n_neighbors))]
            pred_traj    = np.zeros((1,12,nsamples,2))
            for k in range(nsamples):
                # Select one model randomly
                id = random.randint(0,4)
                # Sample a trajectory
                pred_goal_, pred_traj_, _, dist_goal_, dist_traj_ = ensemble[id](my_input_x,neighbors_st=my_neighbors_st,adjacency=my_adjacency,
                             z_mode=False,cur_pos=X_global[:, -1, :cfg.MODEL.DEC_OUTPUT_DIM],
                             first_history_indices=torch.tensor([0],dtype=np.int))
                # Transfer back to global coordinates
                ret = post_process(cfg, X_global, y_global, pred_traj_, pred_goal=pred_goal_, dist_traj=dist_traj_, dist_goal=dist_goal_)
                X_global_, y_global_, pred_goal_, pred_traj_, dist_traj_, dist_goal_ = ret
                pred_traj[0,:,k,:] = pred_traj_[0,:,0,:]

            key =  list(neighbors_un.keys())[0]
            neighbors = neighbors_un[key][0]
            # Visualize
            plt.figure()
            # Observations
            plt.plot(X_global_[0,:,0],X_global_[0,:,1],'red')
            # Neighbors
            for neighbor in neighbors:
                plt.plot(neighbor[:,0],neighbor[:,1],'blue')
            # The samples produced by BitTrap
            for sample in range(pred_traj[0].shape[1]):
                plt.plot([X_global_[0,-1,0],pred_traj[0,0,sample,0]],
                         [X_global_[0,-1,1],pred_traj[0,0,sample,1],],'green')
                plt.plot(pred_traj[0,:,sample,0],pred_traj[0,:,sample,1],'green')
            # The ground truth
            plt.plot(y_global[0,:,0],y_global[0,:,1],'blue')
            plt.axis('equal')
            plt.show()
            break

    ##################################################################
    # With our data (to show how to use BitTrap normalization)
    # Load the dataset and perform the split
    training_data, validation_data, testing_data, test_homography = setup_loo_experiment('ETH_UCY',dataset_dir,dataset_names,args.id_test,experiment_parameters,pickle_dir='pickle',use_pickled_data=False,use_neighbors=True)
    # Torch dataset
    X_test            = np.concatenate([testing_data[OBS_TRAJ],testing_data[OBS_TRAJ_VEL],testing_data[OBS_TRAJ_ACC]],axis=2)
    test_data         = traj_dataset_bitrap(X_test,testing_data[OBS_NEIGHBORS],testing_data[PRED_TRAJ])
    # Form batches
    batched_test_data = torch.utils.data.DataLoader(test_data,batch_size=1,shuffle=False)

    with torch.set_grad_enabled(False):
        for (data_test, neighbors_test, target_test) in batched_test_data:
            # Input: batch_sizex8x6 (un-normalized)
            X_global     = torch.Tensor(data_test).to(cfg.DEVICE)
            # This is the GT: batch_sizex12x2 (un-normalized)
            y_global     = torch.Tensor(target_test)
            # Input: batch_sizex8x6 (**normalized**)
            node_type = env.NodeType[0]
            state     = {'position':['x','y'], 'velocity':['x','y'], 'acceleration':['x','y']}
            _, std    = env.get_standardize_params(state, node_type)
            std[0:2]  = env.attention_radius[(node_type, node_type)]
            # Reference point: the last observed position, batch_size x 6
            rel_state        = np.zeros_like(data_test[:,0])
            rel_state[:,0:2] = np.array(data_test)[:,-1, 0:2]
            rel_state        = np.expand_dims(rel_state,axis=1)
            my_x_st          = env.standardize(data_test,state,node_type,mean=rel_state,std=std)
            my_input_x       = torch.tensor(my_x_st,dtype=torch.float).to(cfg.DEVICE)
            my_neighbors_st = {}
            my_neighbors_st[(node_type,node_type)] = [[]]
            n_neighbors = len(neighbors_test)
            for n in neighbors_test:
                n_st = env.standardize(n, state, node_type, mean=rel_state, std=std)
                my_neighbors_st[(node_type,node_type)][0].append(n_st)
            # Should be a list of x tensors 8x6
            # Dictionary with keys pairs node_type x node_type
            my_adjacency = {}
            my_adjacency[(node_type,node_type)] = [torch.tensor(np.ones(n_neighbors))]
            pred_traj    = np.zeros((1,12,nsamples,2))
            for k in range(nsamples):
                # Select one model randomly
                id = random.randint(0,4)
                # Sample a trajectory
                pred_goal_, pred_traj_, _, dist_goal_, dist_traj_ = ensemble[id](my_input_x,neighbors_st=my_neighbors_st,adjacency=my_adjacency,
                             z_mode=False,cur_pos=X_global[:,-1,:2],
                             first_history_indices=torch.tensor([0],dtype=np.int))
                # Transfer back to global coordinates
                ret = post_process(cfg, X_global, y_global, pred_traj_, pred_goal=pred_goal_, dist_traj=dist_traj_, dist_goal=dist_goal_)
                X_global_, y_global_, pred_goal_, pred_traj_, dist_traj_, dist_goal_ = ret
                pred_traj[0,:,k,:] = pred_traj_[0,:,0,:]

            # Visualize
            plt.figure()
            # Observations
            plt.plot(X_global_[0,:,0],X_global_[0,:,1],'red')
            # Neighbors. When x and y are zero, this means the data is missing, so we should not plot it
            for neighbor in neighbors_test:
                nx       = neighbor[0,:,0]
                ny       = neighbor[0,:,1]
                valid_positions = np.where((nx!=0)|(ny!=0))
                plt.plot(nx[valid_positions],ny[valid_positions],'purple')
            # The samples produced by BitTrap
            for sample in range(pred_traj[0].shape[1]):
                plt.plot([X_global_[0,-1,0],pred_traj[0,0,sample,0]],
                         [X_global_[0,-1,1],pred_traj[0,0,sample,1],],'green')
                plt.plot(pred_traj[0,:,sample,0],pred_traj[0,:,sample,1],'green')
            # The ground truth
            plt.plot(y_global[0,:,0],y_global[0,:,1],'blue')
            plt.axis('equal')
            plt.show()
