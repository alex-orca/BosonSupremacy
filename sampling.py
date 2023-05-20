import numpy as np
import cupy as cp
from tqdm import tqdm
import time
import argparse
import torch
from scipy.linalg import expm
import warnings
import os
os.environ["CUPY_TF32"] = "1"

warnings.filterwarnings("ignore", category=UserWarning)

from decimal import *

from mpi4py import MPI
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
cp.cuda.Device(rank % 4).use()

parser = argparse.ArgumentParser()
parser.add_argument('--d', type=int, help='d for calculating the MPS before random displacement. Maximum number of photons per mode before displacement - 1.')
parser.add_argument('--dd', type=int, help='d for after random displacement. Maximum number of photons per mode that can be sampled - 1.')
parser.add_argument('--chi', type=int, help='Bond dimension.')
parser.add_argument('--rpn', type=int, help='Ranks per node. Should be the number of gpus available.')
args = vars(parser.parse_args())

d = args['d']
dd = args ['dd']
chi = args['chi']
rpn = args['rpn']

def nothing_function(object):
    return object

if rank != 0:
    tqdm = nothing_function


def sampling(path, dd, Lambda, sqrtW, samples_in_parallel, compare=False):
    if rank == 0:
        Gamma = np.load(path + f'Gamma_{0}.npy')
        print('ChiL: {}, d: {}.'.format(Gamma.shape[0], Gamma.shape[2]))
        d = Gamma.shape[2]
        M = len(sqrtW) // 2
    else:
        d = M = None
    print('Communicating')
    d = comm.bcast(d, root=0)
    M = comm.bcast(M, root=0)
    print('Done communicating')
    np_Lambda = Lambda
    # np_Gamma = Gamma
    Lambda = cp.array(Lambda)
    if rank == 0:
        print('Generating random displacements')
    random_array = np.random.normal(size=(2 * M, samples_in_parallel))
    
    np_pure_mu = sqrtW @ random_array[:, 0]
    np_pure_alpha = mu_to_alpha(np_pure_mu, hbar=2)
    np_displacements = displaces(dd, np_pure_alpha)

    pure_mu = sqrtW @ random_array
    pure_mu = pure_mu.T
    pure_alpha = batch_mu_to_alpha(pure_mu, hbar=2)
    displacements = batch_displaces(dd, pure_alpha)

    np_res = []
    res = []
    #Sample
    for i in tqdm(range(M)):
        # if rank == 0:
        #     print(i)
        if i == 0:

            random_thresholds = cp.array(np.random.rand(samples_in_parallel, 1)) # samples_in_parallel

            if compare:
                np_probs = []
                np_Gamma_temp = np_Gamma[:, :, :, 0] @ np_displacements[0].T
                np_temp_lambda = np_Lambda[:, 0]
                np_probs = [np.dot(np.abs(np.sum(np_Gamma_temp[:, :, j], axis = 0)) ** 2, np_temp_lambda ** 2) for j in range(d)]
                np_probs = np.array(np_probs) / np.sum(np_probs);
                # print('np probs ', np_probs)
                np_res.append(np.sum(np.cumsum(np_probs) < random_thresholds[0].item()))
                # res.append(np.random.choice(np.arange(d), p = probs))
                np_pre_tensor = np.copy(np_Gamma_temp[:, :, np_res[0]]);

            probs = []
            if rank % rpn == 0:
                Gamma = np.ascontiguousarray(Gamma, dtype='complex64') # chi x chi x cutoff
                for target_rank in range(rank + 1, rank + rpn, 1):
                    comm.Send([Gamma, MPI.C_FLOAT_COMPLEX], target_rank, tag=0)
            else:
                Gamma = np.zeros([chi, chi, d], dtype='complex64')
                comm.Recv([Gamma, MPI.C_FLOAT_COMPLEX], source=rank // rpn * rpn, tag=0)
            temp_tensor = cp.zeros([chi, chi, dd], dtype='complex64')
            temp_tensor[:, :, :d] = cp.array(Gamma)
            temp_tensor = cp.array(temp_tensor)
            temp_tensor = cp.sum(temp_tensor, axis=0) # chi x cutoff
            temp_tensor = cp.einsum('mj,Bkj->Bmk', temp_tensor, displacements[:, i])
            pre_tensor = cp.copy(temp_tensor)
            temp_tensor = cp.abs(temp_tensor) ** 2
            probs = [cp.dot(temp_tensor[:, :, j], Lambda[:, 0] ** 2) for j in range(dd)]
            probs = cp.array(probs).T
            probs = probs / cp.sum(probs, axis=1)[:, np.newaxis]
            cumulative_probs = cp.cumsum(probs, axis=1)
            random_thresholds = cp.repeat(random_thresholds, dd, axis=1) # samples_in_parallel x cutoff
            has_more_photons = random_thresholds > cumulative_probs # samples_in_parallel x cutoff
            n_photons = cp.sum(has_more_photons, axis=1)
            res.append(n_photons)
            batch_to_n_ph = cp.zeros([samples_in_parallel, dd], dtype=int)
            for n_ph in range(dd):
                batch_to_n_ph[cp.where(n_photons == n_ph)[0], n_ph] = 1
            pre_tensor = cp.einsum('BmP, BP -> Bm', pre_tensor, batch_to_n_ph)
        else:

            if compare:
                np_probs = [];
                np_tensor = np_pre_tensor * np_Lambda[:, len(np_res) - 1]
                np_Gamma_temp = np_Gamma[:, :, :, len(np_res)] @ np_displacements[i].T

            probs = []
            tensor = pre_tensor * Lambda[:, len(res) - 1] # samples_in_parallel x chi
            if rank % rpn == 0:
                Gamma = np.load(path + f'Gamma_{i}.npy')
                Gamma = np.ascontiguousarray(Gamma, dtype='complex64') # chi x chi x cutoff
                for target_rank in range(rank + 1, rank + rpn, 1):
                    comm.Send([Gamma, MPI.C_FLOAT_COMPLEX], target_rank, tag=0)
            else:
                Gamma = np.zeros([chi, chi, d], dtype='complex64')
                comm.Recv([Gamma, MPI.C_FLOAT_COMPLEX], source=rank // rpn * rpn, tag=0)
            Gamma_temp = cp.zeros([chi, chi, dd], dtype='complex64')
            Gamma_temp[:, :, :d] = cp.array(Gamma)
            temp_tensor = cp.copy(tensor) # samples_in_parallel x chi
            temp_tensor = cp.einsum('Bn,nmj->Bmj', temp_tensor, Gamma_temp) # samples_in_parallel x chi x cutoff
            temp_tensor = cp.einsum('Bmj,Bkj->Bmk', temp_tensor, displacements[:, i])
            pre_tensor = cp.copy(temp_tensor)
            temp_tensor = cp.abs(temp_tensor) ** 2

            for j in range(dd):

                if compare:
                    np_temp_tensor = np.copy(np_tensor);
                    np_temp_tensor = np_temp_tensor @ np_Gamma_temp[:, :, j];
                    if len(np_res) == M - 1:
                        np_probs.append(np.abs(np.sum(np_temp_tensor)) ** 2)
                    else:
                        np_probs.append(np.dot(np.abs(np.sum(np_temp_tensor, axis = 0)) ** 2, np_Lambda[:, len(np_res)] ** 2)); # sum of probs is not 1, but the prob of previous results

                if len(res) == M - 1:
                    probs.append(temp_tensor[:, 0, j])
                else:
                    probs.append(cp.dot(temp_tensor[:, :, j], Lambda[:, len(res)] ** 2)); # appending shape samples_in_parallel
            
            random_thresholds = cp.array(np.random.rand(samples_in_parallel, 1)) # samples_in_parallel

            if compare:
                np_probs = np.array(np_probs) / np.sum(np_probs);     
                np_res.append(np.sum(np.cumsum(np_probs) < random_thresholds[0].item()))

            probs = cp.array(probs).T # samples_in_parallel x cutoff
            probs = probs / cp.sum(probs, axis=1)[:, np.newaxis] # samples_in_parallel x cutoff
            cumulative_probs = cp.cumsum(probs, axis=1) # samples_in_parallel x cutoff
            random_thresholds = cp.array(random_thresholds)
            random_thresholds = cp.repeat(random_thresholds, dd, axis=1) # samples_in_parallel x cutoff
            has_more_photons = random_thresholds > cumulative_probs # samples_in_parallel x cutoff
            n_photons = cp.sum(has_more_photons, axis=1) # samples_in_parallel
            res.append(n_photons)

            if i == M - 1:
                break
            
            if compare:
                np_pre_tensor = np_tensor @ np_Gamma_temp[:, :, np_res[-1]]

            batch_to_n_ph = cp.zeros([samples_in_parallel, dd], dtype=int)
            for n_ph in range(dd):
                batch_to_n_ph[cp.where(n_photons == n_ph)[0], n_ph] = 1
            pre_tensor = cp.einsum('BmP, BP -> Bm', pre_tensor, batch_to_n_ph)

    results = cp.asnumpy(cp.array(res).T)
    if compare:
        print(np_res, results[0])

    return results


def destroy(N):
    data = np.sqrt(np.arange(1, N, dtype=complex))
    return np.diag(data, 1);

def displace(N, alpha): # N is the dim
    a = destroy(N)
    return expm(alpha * np.conj(a).T - np.conj(alpha) * a)

def displaces(N, alphas): # N is the dim
    a = destroy(N)
    res = np.array([expm(alpha * np.conj(a).T - np.conj(alpha) * a) for alpha in alphas]);
    return np.array(res)

# def gpu_expm(mat):
#     mat = torch.tensor(mat, dtype=torch.complex64).cuda()
#     eigvals, eigvecs = torch.linalg.eig(mat)
#     eigvals = torch.exp(eigvals)
#     diag = eigvals.unsqueeze(1) * torch.eye(mat.shape[1], device='cuda')
#     return (eigvecs @ diag @ torch.permute(eigvecs.conj(), (0, 2, 1))).cpu().numpy()

def batch_displaces(N, alphas): # N is the dim
    samples_in_parallel = alphas.shape[0]
    # alphas = alphas.reshape(-1, 1, 1)
    M = alphas.shape[1]
    a = destroy(N)
    a_h = np.conj(a).T
    a = np.repeat(a[np.newaxis], samples_in_parallel, axis=0)
    a_h = np.repeat(a_h[np.newaxis], samples_in_parallel, axis=0)
    results = []
    for i in tqdm(range(M)):
        alpha = alphas[:, i].reshape(-1, 1, 1)
        results.append(torch.linalg.matrix_exp(torch.tensor(alpha * a_h - np.conj(alpha) * a).cuda()).cpu().numpy())
        # results.append(expm(alpha * a_h - np.conj(alpha) * a))
    results = np.transpose(np.array(results), (1, 0, 2, 3))
    print(results.shape)
    return results

def mu_to_alpha(mu, hbar=2):
    M = len(mu) // 2
    # mean displacement of each mode
    alpha = (mu[:M] + 1j * mu[M:]) / np.sqrt(2 * hbar)
    return alpha

def batch_mu_to_alpha(mu, hbar=2):
    M = mu.shape[1] // 2
    alpha = (mu[:, :M] + 1j * mu[:, M:]) / np.sqrt(2 * hbar)
    return alpha



if __name__ == "__main__":
    
    rootdir = '/home/minzhaoliu/BosonSupremacy/data_S15/'
    path = rootdir + f'd_{d}_chi_{chi}/'
    sq_array = np.load(rootdir + "sq_array.npy")
    sq_cov = np.load(rootdir + "sq_cov.npy")
    cov = np.load(rootdir + "cov.npy")
    thermal_cov = cov - sq_cov;
    thermal_cov = thermal_cov + 1.000001 * np.eye(len(thermal_cov)) * np.abs(np.min(np.linalg.eigvalsh(thermal_cov)))
    sqrtW = np.linalg.cholesky(thermal_cov)
    M = sqrtW.shape[0] // 2
    Lambda = np.zeros([chi, M - 1], dtype='float32')
    for i in range(M - 1):
        Lambda[:, i] = np.load(path + f"Lambda_{i}.npy")
    samples_per_rank = 5000
    
    samples = np.zeros([0, M], dtype='int8')
    # samples = np.load(rootdir + f"samples_d_{d}_dd_{dd}_chi_{chi}_{rank}.npy")
    for subsamples in range(1):
        subsamples = sampling(path, dd, Lambda, sqrtW, samples_per_rank, False)
        samples = np.concatenate([samples, subsamples], axis=0)
        np.save(rootdir + f"samples_d_{d}_dd_{dd}_chi_{chi}_{rank}.npy", samples)
        print(rank, samples.shape, samples.mean(), samples[:, 0].mean(), samples[:, -1].mean())
    # np.save(rootdir + f"samples_d_{d}_chi_{chi}_{rank}.npy", samples)