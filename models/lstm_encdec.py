import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

def convertToCov(sx,sy,corr):
    # Exponential to get a positive value for variances
    sx   = torch.exp(sx)+1e-2
    sy   = torch.exp(sy)+1e-2
    sxsy = torch.sqrt(sx*sy)
    # tanh to get a value between [-1, 1] for correlation
    corr = torch.tanh(corr)
    # Covariance
    cov  = sxsy*corr
    return sx,sy,cov

def Gaussian2DLikelihood(targets, means, sigmas, dt):
    '''
    Computes the likelihood of predicted locations under a bivariate Gaussian distribution
    params:
    targets: Torch variable containing tensor of shape [nbatch, 12, 2]
    means: Torch variable containing tensor of shape [nbatch, 12, 2]
    sigmas:  Torch variable containing tensor of shape [nbatch, 12, 3]
    '''
    # Extract mean, std devs and correlation
    mux, muy, sx, sy, corr = means[:, 0], means[:, 1], sigmas[:, :, 0], sigmas[:,:,1], sigmas[:,:,2]
    sx,sy,cov = convertToCov(sx, sy, corr)
    # Variances and covariances are summed along time.
    # They are also scaled to fit displacements instead of velocities.
    sx   = dt*dt*sx.sum(1)
    sy   = dt*dt*sy.sum(1)
    cov  = dt*dt*cov.sum(1)
    # Compute factors
    normx= targets[:, 0] - mux
    normy= targets[:, 1] - muy
    det  = sx*sy-cov*cov
    z    = torch.pow(normx,2)*sy/det + torch.pow(normy,2)*sx/det - 2*cov*normx*normy/det
    result = 0.5*(z+torch.log(det))
    # Compute the loss across all frames and all nodes
    loss = result.sum()
    return(loss)

# A simple encoder-decoder network for HTP
class lstm_encdec_gaussian(nn.Module):
    def __init__(self, in_size, embedding_dim, hidden_dim, output_size):
        super(lstm_encdec_gaussian, self).__init__()

        # Layers
        self.embedding = nn.Linear(in_size, embedding_dim)
        self.drop1     = nn.Dropout(p=0.4)
        self.lstm1     = nn.LSTM(embedding_dim, hidden_dim)
        self.lstm2     = nn.LSTM(embedding_dim, hidden_dim)
        self.drop2     = nn.Dropout(p=0.4)
        # Added outputs for  sigmaxx, sigmayy, sigma xy
        self.decoder   = nn.Linear(hidden_dim, output_size + 3)
        self.dt        = 0.4

    # Encoding of the past trajectory
    def encode(self, V):
        # Last position traj
        v_last = V[:,-1,:].view(len(V), 1, -1)
        # Embedding positions [batch, seq_len, input_size]
        emb = self.drop1(self.embedding(V))
        # LSTM for batch [seq_len, batch, input_size]
        lstm_out, hidden_state = self.lstm1(emb.permute(1,0,2))
        return v_last,hidden_state

    # Decoding the next spatial data
    def decode(self, last_v, hidden_state):
        # Embedding last spatial data
        emb_last = self.embedding(last_v)
        # lstm for last spatial data with hidden states from batch
        lstm_out, hidden_state = self.lstm2(emb_last.permute(1,0,2), hidden_state)
        # Decoder and Prediction
        dec      = self.decoder(self.drop2(hidden_state[0].permute(1,0,2)))
        # Model evaluates deviation to the linear model
        pred_v = dec[:,:,:2] + last_v
        sigma_v= dec[:,:,2:]
        return pred_v,sigma_v,hidden_state

    def forward(self,obs_vels,target_vels,obs_abs,target_abs,teacher_forcing=False):
        # Encode the past trajectory (sequence of displacements)
        last_vel,hidden_state = self.encode(obs_vels)

        loss        = 0
        pred_vels   = []
        sigma_vels  = []

        # Decode the future trajectories
        for i, target_vel in enumerate(target_vels.permute(1,0,2)):
            # Decode last displacement and hidden state into new velocity
            pred_vel,sigma_vel,hidden_state = self.decode(last_vel,hidden_state)
            # Keep displacement and variance on velocity
            pred_vels.append(pred_vel)
            sigma_vels.append(sigma_vel)
            # Update the last position
            if teacher_forcing:
                # With teacher forcing, use the GT displacement
                last_vel = target_vel.view(len(target_vel), 1, -1)
            else:
                # Otherwise, use the predicted displacement we just did
                last_vel = pred_vel
            # Deduce absolute position by summing all our predicted displacements to
            # the last absolute position
            pred_abs = obs_abs[:,-1,:] + torch.mul(torch.cat(pred_vels, dim=1).sum(1),self.dt)
            # Evaluate likelihood
            loss += Gaussian2DLikelihood(target_abs[:,i,:], pred_abs, torch.cat(sigma_vels, dim=1),self.dt)
        # Return total loss
        return loss

    def predict(self, obs_vels, dim_pred= 1):
        # Encode the past trajectory
        last_vel,hidden_state = self.encode(obs_vels)

        pred_vels  = []
        sigma_vels = []

        for i in range(dim_pred):
            # Decode last position and hidden state into new position
            pred_vel,sigma_vel,hidden_state = self.decode(last_vel,hidden_state)
            # Keep new displacement and the corresponding variance
            pred_vels.append(pred_vel)
            # Convert sigma_displ into real variances
            sigma_vel[:,:,0],sigma_vel[:,:,1],sigma_vel[:,:,2] = convertToCov(sigma_vel[:,:,0], sigma_vel[:,:,1], sigma_vel[:,:,2])
            sigma_vels.append(sigma_vel)
            # Update the last displacement
            last_vel = pred_vel

        # Sum the displacements and the variances to get the relative trajectory
        pred_traj = self.dt*torch.cumsum(torch.cat(pred_vels, dim=1), dim=1).detach().cpu().numpy()
        sigma_traj= self.dt*self.dt*torch.cumsum(torch.cat(sigma_vels, dim=1), dim=1).detach().cpu().numpy()
        return pred_traj,sigma_traj

# A simple encoder-decoder network for HTP
class lstm_encdec(nn.Module):
    def __init__(self, in_size, embedding_dim, hidden_dim, output_size):
        super(lstm_encdec, self).__init__()
        # Layers
        self.embedding = nn.Linear(in_size, embedding_dim)
        self.drop1     = nn.Dropout(p=0.4)
        self.lstm1     = nn.LSTM(embedding_dim, hidden_dim)
        self.lstm2     = nn.LSTM(embedding_dim, hidden_dim)
        self.drop2     = nn.Dropout(p=0.4)
        self.decoder   = nn.Linear(hidden_dim, output_size)
        # Loss function
        self.loss_fun  = nn.MSELoss()
        self.dt        = 0.4

    # Encoding of the past trajectory data
    def encode(self, V):
        # Last spatial data
        v_last = V[:,-1,:].view(len(V), 1, -1)
        # Embedding spatial data
        emb = self.drop1(self.embedding(V))
        # LSTM for batch [seq_len, batch, input_size]
        lstm_out, hidden_state = self.lstm1(emb.permute(1,0,2))
        return v_last,hidden_state

    # Decoding the next future displacements
    def decode(self, v_last, hidden_state):
        # Embedding last spatial data
        emb_last = self.embedding(v_last)
        # lstm for last spatial data with hidden states from batch
        lstm_out, hidden_state = self.lstm2(emb_last.permute(1,0,2), hidden_state)
        # Decoder and Prediction
        dec = self.decoder(self.drop2(hidden_state[0].permute(1,0,2)))
        # Model evaluates deviation to the linear model
        t_pred = dec + v_last
        return t_pred,hidden_state

    def forward(self,obs_vels,target_vels,obs_abs,target_abs,teacher_forcing=False):
        # Encode the past trajectory (sequence of velocities)
        last_vel,hidden_state = self.encode(obs_vels)
        loss        = 0
        pred_vels = []
        # Decode the future trajectories
        for i, target_vel in enumerate(target_vels.permute(1,0,2)):
            # Decode last displacement and hidden state into new displacement
            pred_vel,hidden_state = self.decode(last_vel,hidden_state)
            # Keep displacement and variance on displacement
            pred_vels.append(pred_vel)
            # Update the last position
            if teacher_forcing:
                # With teacher forcing, use the GT displacement
                last_vel = target_vel.view(len(target_vel), 1, -1)
            else:
                # Otherwise, use the predicted displacement we just did
                last_vel = pred_vel
            # Deduce absolute position by summing all our predicted displacements to
            # the last absolute position
            pred_abs = obs_abs[:,-1,:] + torch.mul(torch.cat(pred_vels, dim=1).sum(1),self.dt)
            # Evaluate likelihood
            loss += self.loss_fun(target_abs[:,i,:],pred_abs)
        # Return total loss
        return loss

    def predict(self, obs_vels, dim_pred= 1):
        # Encode the past trajectory (sequence of velocities)
        last_vel,hidden_state = self.encode(obs_vels)
        pred_vels  = []

        for i in range(dim_pred):
            # Decode last velocity and hidden state into new position
            pred_vel,hidden_state = self.decode(last_vel,hidden_state)
            # Keep new velocity
            pred_vels.append(pred_vel)
            # Update the last velocity
            last_vel = pred_vel

        # Sum the displacements to get the relative trajectory
        return self.dt*torch.cumsum(torch.cat(pred_vels, dim=1), dim=1).detach().cpu().numpy()
