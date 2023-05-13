import numpy as np
import cupy as cp
from tqdm import tqdm
from scipy.special import factorial
from scipy.linalg import sqrtm, svd, block_diag, schur
from math import ceil
import argparse
import time
import os
from filelock import FileLock
from mpi4py import MPI

parser = argparse.ArgumentParser()
parser.add_argument('--d', type=int, help='d for calculating the MPS before random displacement. Maximum number of photons per mode before displacement - 1.')
parser.add_argument('--chi', type=int, help='Bond dimension.')
args = vars(parser.parse_args())

d = args['d']
chi = args['chi']

comm = MPI.COMM_WORLD
rank = comm.Get_rank()

kernel_file = open('direct_mps_kernels.cu')
kernel_string = kernel_file.read()
kernel_file.close()
sigma_select = cp.RawKernel(kernel_string, 'sigma_select')

def Sigma_select(Sigma, target):
    max_blocks = 65535
    n_batch, n_select = target.shape
    n_len = Sigma.shape[0]
    target = cp.array(target, dtype='int32')
    Sigma = cp.array(Sigma, dtype='complex128')
    Sigma2 = cp.zeros([n_batch, n_select, n_select], dtype='complex128')
    threadsperblock = (4, 4, 16)
    blockspergrid = (ceil(n_select/4), ceil(n_select/4), ceil(n_batch/16))
    for superblock_id in range(blockspergrid[2] // max_blocks + 1):
        begin_block = superblock_id * max_blocks
        end_block = min((superblock_id + 1) * max_blocks, blockspergrid[2])
        begin_batch = begin_block * 16
        end_batch = min(n_batch, end_block * 16)
        launch_n_batch = end_batch - begin_batch
        launch_blockspergrid = (blockspergrid[0], blockspergrid[1], end_block - begin_block)
        launch_idx = target[begin_batch : end_batch]
        launch_Sigma2 = Sigma2[begin_batch : end_batch]
        # print(launch_idx, Sigma, launch_blockspergrid, threadsperblock, begin_batch, end_batch)
        sigma_select(launch_blockspergrid, threadsperblock, (launch_n_batch, n_select, n_len, Sigma, launch_idx, launch_Sigma2))
    return Sigma2
    
def push_to_end(array):
    n_batch, n_select = array.shape
    new_array = np.zeros_like(array)
    idx = np.zeros(n_batch, dtype='int32')
    for i in range(1, n_select + 1):
        occupied = array[:, -i] != 0
        idx += occupied
        new_array[np.arange(n_batch), -idx] = array[:, -i]
    return new_array

def sympmat(N, dtype=np.float64):
    I = np.identity(N, dtype=dtype)
    O = np.zeros_like(I, dtype=dtype)
    S = np.block([[O, I], [-I, O]])
    return S

def xpxp_to_xxpp(S):
    shape = S.shape
    n = shape[0]

    if n % 2 != 0:
        raise ValueError("The input array is not even-dimensional")

    n = n // 2
    ind = np.arange(2 * n).reshape(-1, 2).T.flatten()

    if len(shape) == 2:
        if shape[0] != shape[1]:
            raise ValueError("The input matrix is not square")
        return S[:, ind][ind]

    return S[ind]

def williamson(V, tol=1e-11):
    (n, m) = V.shape

    if n != m:
        raise ValueError("The input matrix is not square")

    diffn = np.linalg.norm(V - np.transpose(V))

    if diffn >= tol:
        raise ValueError("The input matrix is not symmetric")

    if n % 2 != 0:
        raise ValueError("The input matrix must have an even number of rows/columns")

    n = n // 2
    omega = sympmat(n)
    vals = np.linalg.eigvalsh(V)

    for val in vals:
        if val <= 0:
            raise ValueError("Input matrix is not positive definite")

    Mm12 = sqrtm(np.linalg.inv(V)).real
    r1 = Mm12 @ omega @ Mm12
    s1, K = schur(r1)
    X = np.array([[0, 1], [1, 0]])
    I = np.identity(2)
    seq = []

    for i in range(n):
        if s1[2 * i, 2 * i + 1] > 0:
            seq.append(I)
        else:
            seq.append(X)

    p = block_diag(*seq)
    Kt = K @ p
    s1t = p @ s1 @ p
    dd = xpxp_to_xxpp(s1t)
    perm_indices = xpxp_to_xxpp(np.arange(2 * n))
    Ktt = Kt[:, perm_indices]
    Db = np.diag([1 / dd[i, i + n] for i in range(n)] + [1 / dd[i, i + n] for i in range(n)])
    S = Mm12 @ Ktt @ sqrtm(Db)
    return Db, np.linalg.inv(S).T

def hafnian(A): 

    matshape = A.shape[1:]
    n_batch = A.shape[0]

    if matshape == (0, 0):
        return cp.ones(n_batch, dtype='complex64')
    
    if matshape[0] % 2 != 0:
        return cp.zeros(n_batch, dtype='complex64')
    
    '''removed case where it is identity'''
    if matshape[0] == 2:
        return A[:, 0, 1]

    if matshape[0] == 3:
        return (
            A[:, 0, 0] * A[:, 1, 2] + A[:, 1, 1] * A[:, 0, 2] + A[:, 2, 2] * A[:, 0, 1] + A[:, 0, 0] * A[:, 1, 1] * A[:, 2, 2]
        )

    if matshape[0] == 4:
        return A[:, 0, 1] * A[:, 2, 3] + A[:, 0, 2] * A[:, 1, 3] + A[:, 0, 3] * A[:, 1, 2]

    return recursive_hafnian(A)


def recursive_hafnian(A):  # pragma: no cover

    n_batch, nb_lines, nb_columns = A.shape
    if nb_lines != nb_columns:
        raise ValueError("Matrix must be square")

    if nb_lines % 2 != 0:
        raise ValueError("Matrix size must be even")

    n = A.shape[1] // 2
    z = cp.zeros((n_batch, n * (2 * n - 1), n + 1), dtype=A.dtype)
    for j in range(1, 2 * n):
        ind = j * (j - 1) // 2
        for k in range(j):
            z[:, ind + k, 0] = A[:, j, k]
    g = cp.zeros([n_batch, n + 1], dtype=A.dtype)
    g[:, 0] = 1
    return solve(z, 2 * n, 1, g, n)


def solve(b, s, w, g, n):  # pragma: no cover

    n_batch = b.shape[0]
    if s == 0:
        return w * g[:, n]
    c = cp.zeros((n_batch, (s - 2) * (s - 3) // 2, n + 1), dtype=g.dtype)
    i = 0
    for j in range(1, s - 2):
        for k in range(j):
            c[:, i] = b[:, (j + 1) * (j + 2) // 2 + k + 2]
            i += 1
    h = solve(c, s - 2, -w, g, n)
    e = g.copy()
    for u in range(n):
        for v in range(n - u):
            e[:, u + v + 1] += g[:, u] * b[:, 0, v]
    for j in range(1, s - 2):
        for k in range(j):
            for u in range(n):
                for v in range(n - u):
                    c[:, j * (j - 1) // 2 + k, u + v + 1] += (
                        b[:, (j + 1) * (j + 2) // 2, u] * b[:, (k + 1) * (k + 2) // 2 + 1, v]
                        + b[:, (k + 1) * (k + 2) // 2, u] * b[:, (j + 1) * (j + 2) // 2 + 1, v]
                    )
    return h + solve(c, s - 2, w, e, n)

def blochmessiah(S):
    N, _ = S.shape

    # Changing Basis
    R = (1 / np.sqrt(2)) * np.block(
        [[np.eye(N // 2), 1j * np.eye(N // 2)], [np.eye(N // 2), -1j * np.eye(N // 2)]]
    )
    Sc = R @ S @ np.conjugate(R).T
    # Polar Decomposition
    # u1, d1, v1 = np.linalg.svd(Sc)
    u1, d1, v1 = svd(Sc, lapack_driver='gesvd')
    Sig = u1 @ np.diag(d1) @ np.conjugate(u1).T
    Unitary = u1 @ v1
    # Blocks of Unitary and Hermitian symplectics
    alpha = Unitary[0 : N // 2, 0 : N // 2]
    beta = Sig[0 : N // 2, N // 2 : N]
    # Bloch-Messiah in this Basis
    u2, d2, v2 = np.linalg.svd(beta)
    sval = np.arcsinh(d2)
    takagibeta = u2 @ sqrtm(np.conjugate(u2).T @ (v2.T))
    uf = np.block([[takagibeta, 0 * takagibeta], [0 * takagibeta, np.conjugate(takagibeta)]])
    vf = np.block(
        [
            [np.conjugate(takagibeta).T @ alpha, 0 * takagibeta],
            [0 * takagibeta, np.conjugate(np.conjugate(takagibeta).T @ alpha)],
        ]
    )
    df = np.block(
        [
            [np.diag(np.cosh(sval)), np.diag(np.sinh(sval))],
            [np.diag(np.sinh(sval)), np.diag(np.cosh(sval))],
        ]
    )
    # Rotating Back to Original Basis
    uff = np.conjugate(R).T @ uf @ R
    vff = np.conjugate(R).T @ vf @ R
    dff = np.conjugate(R).T @ df @ R
    dff = np.real_if_close(dff)
    vff = np.real_if_close(vff)
    uff = np.real_if_close(uff)
    return uff, dff, vff

def thermal_photons(nth, cutoff = 20):
    return 1 / (nth + 1) * (nth / (nth + 1)) ** np.arange(cutoff)

def cartesian(array1, array2):
    # array1_orig = array1
    # array2_orig = array2
    array1 = np.array(array1)
    array2 = np.array(array2)
    if len(array1.shape) == 1:
        array1 = array1.reshape(-1, 1)
    if len(array2.shape) == 1:
        array2 = array2.reshape(-1, 1)
    len1 = array1.shape[0]
    len2 = array2.shape[0]
    array1 = np.repeat(array1, len2, 0)
    array2 = np.tile(array2, (len1, 1))
    # assert np.allclose(np.concatenate([array1, array2], axis=1), np.array([np.append(a, b) for a in array1_orig for b in array2_orig]))
    return np.concatenate([array1, array2], axis=1)

    # return np.array([np.append(a, b) for a in array1 for b in array2])


def get_cumsum_kron(sq_cov, L, chi = 100, max_dim = 10 ** 5, cutoff = 6, err_tol = 10 ** (-12)):
    M = len(sq_cov) // 2
    mode = np.arange(L, M)
    modes = np.append(mode, mode + M)
    sq_cov_A = sq_cov[np.ix_(modes, modes)]

    D, S = williamson(sq_cov_A)
    d = (np.diag(D) - 1) / 2

    d[d < 0] = 0

    res = thermal_photons(d[0], cutoff)
    num = np.arange(cutoff, dtype='int8')
    
    kron_time = 0
    cart_time = 0
    select_time = 0
    sort_time = 0
    rev_time = 0
    
    for i in range(1, M - L):
        start = time.time()
        res = np.kron(res, thermal_photons(d[i], cutoff))
        kron_time += time.time() - start
        start = time.time()
        keep_idx = np.where(res > err_tol)[0]
        start = time.time()
        if len(num.shape) == 1:
            num = num.reshape(-1, 1)
        num = np.concatenate([num[keep_idx // cutoff], np.arange(cutoff).reshape(-1, 1)[keep_idx % cutoff]], axis=1)
        cart_time += time.time() - start
        res = res[keep_idx]
        select_time += time.time() - start
        start = time.time()
        idx = np.argsort(res)[-min(len(res), max_dim):]       
        sort_time += time.time() - start
        start = time.time()
        res = res[idx][::-1]
        num = num[idx][::-1]
        rev_time += time.time() - start

    print('loop time ', kron_time, cart_time, select_time, sort_time, rev_time)
            
    len_ = min(chi, len(res))
    idx = np.argsort(res)[-len_:]
    idx_sorted = idx[np.argsort(res[idx])]
    res = res[idx_sorted][::-1]
    num = num[idx_sorted][::-1]
    
    # res = cp.asnumpy(res)
    # num = cp.asnumpy(num)
    print(res.shape, num.shape)

    return res, num, S

def get_Sigma(U2, sq, U1):
    M = len(sq)
    Sigma = np.zeros((2 * M, 2 * M), dtype = complex)
    Sigma[:M, :M] = U2 @ np.diag(np.tanh(sq)) @ U2.T
    Sigma[:M, M:] = U2 @ np.diag(1 / np.cosh(sq)) @ U1
    Sigma[M:, :M] = U1.T @ np.diag(1 / np.cosh(sq)) @ U2.T
    Sigma[M:, M:] = -U1.T @ np.diag(np.tanh(sq)) @ U1
    return Sigma.astype('complex64')

def get_target(num):
    n_select = np.sum(num, axis=1).max()
    n_batch, n_len = num.shape
    idx_x = np.tile(np.arange(n_select, dtype='int32').reshape(1, -1), (n_batch, 1))
    target = np.zeros([n_batch, n_select], dtype='int32')
    idx_end = np.zeros(n_batch)
    idx_begin = np.zeros(n_batch)
    num = np.array(num)
    for i in range(n_len):
        vals_n = num[:, i]
        # vals_n = cp.array(n_[:, i])
        idx_end += vals_n
        # print(idx_begin, idx_end)
        mask = idx_x >= idx_begin.reshape(-1, 1)
        mask *= idx_x < idx_end.reshape(-1, 1)
        # print(mask)
        target += mask * (i + 1)
        idx_begin = np.copy(idx_end)
    return cp.array(target, dtype='int32')

def A_elem(Sigma, target, denominator, max_memory_in_gb):
    print(target.shape)
    n_batch, n_select = target.shape
    all_haf = cp.zeros([0], dtype='complex64')
    n_batch_max = int(max_memory_in_gb * (10 ** 9) // (n_select ** 2 * 8))
    print(n_batch_max)
    sigma_time = 0
    haf_time = 0
    for begin_batch in tqdm(range(0, n_batch, n_batch_max)):
        end_batch = min(n_batch, begin_batch + n_batch_max)
        start = time.time()
        Sigma2 = Sigma_select(Sigma, target[begin_batch : end_batch])
        sigma_time += time.time() - start
        start = time.time()
        haf = hafnian(Sigma2).astype('complex64')
        haf_time += time.time() - start
        # haf = cp.zeros([Sigma2.shape[0]], dtype='complex64')
        cp.cuda.runtime.deviceSynchronize()
        # print(haf)
        all_haf = cp.append(all_haf, haf)
    return all_haf / denominator, haf_time, sigma_time

def get_U2_sq_U1(S_l, S_r):
    M = len(S_r) // 2
    mode = np.arange(M - 1) + 1
    modes = np.append(mode, mode + M)
    
    S_l2_inv = np.eye(2 * M, dtype = float)
    S_l2_inv[np.ix_(modes, modes)] = np.linalg.inv(S_l)
    S = S_l2_inv @ S_r
    
    S2, SQ, S1 = blochmessiah(S)
    U2 = S2[:M, :M] - 1j * S2[:M, M:]
    U1 = S1[:M, :M] - 1j * S1[:M, M:]

    sq = np.log(np.diag(SQ)[:M])
    
    return U2, sq, U1




if __name__ == "__main__":

    rootdir = "/project2/liangjiang/mliu6/DirectMPS/data_4/"
    path = rootdir + f"d_{d}_chi_{chi}/"
    sq_cov = np.load(rootdir + "sq_cov.npy")
    cov = np.load(rootdir + "cov.npy")
    sq_array = np.load(rootdir + "sq_array.npy")
    M = len(cov) // 2

    if not os.path.isfile(path + 'active_MPS_sites.npy'):
        if rank == 0:
            if not os.path.isdir(path):
                os.mkdir(path)
            active_sites = np.zeros(M, dtype='int32')
            np.save(path + 'active_MPS_sites.npy', active_sites)

        completed = True
        comm.bcast(completed, root=0)

    while True:

        with FileLock(path + 'active_MPS_sites.npy.lock'):
            print(f'Rank {rank} acquired lock.')
            active_sites = np.load(path + 'active_MPS_sites.npy')
            uncomputed_sites = np.where(active_sites == 0)[0]
            if uncomputed_sites.shape[0] == 0:
                print(f'Rank {rank} all completed.')
                quit()
            compute_site = uncomputed_sites[0]
            active_sites[compute_site] = 1
            np.save(path + 'active_MPS_sites.npy', active_sites)
            print(f'Computing site {compute_site}.')

        real_start = time.time()

        max_memory_in_gb = 0.5
        max_dim = 10 ** 5; err_tol = 10 ** (-10)
        tot_haf_time = 0
        tot_kron_time = 0

        _, S_r = williamson(sq_cov)

        Gamma = np.zeros([chi, chi, d], dtype='complex64')
        Lambda = cp.zeros([chi], dtype='float32')



        if compute_site == 0:

            res = np.load(path + f'res_{compute_site}.npy')
            num = np.load(path + f'num_{compute_site}.npy')
            S_l = np.load(path + f'S_{compute_site}.npy')
            num = num[res > err_tol]
            res = res[res > err_tol]
            U2, sq, U1 = get_U2_sq_U1(S_l, S_r)
            Sigma = get_Sigma(U2, sq, U1)
            left_target = get_target(num)
            left_sum = np.sum(num, axis=1)
            left_denominator = cp.sqrt(cp.product(cp.array(factorial(num)), axis=1))
            Z = np.sqrt(np.prod(np.cosh(sq)))
            Lambda[:len(res)] = cp.array(np.sqrt(res))
            for j in np.arange(d):
                for size in np.arange(np.max(left_sum) + 1):
                    left_idx = np.where(left_sum == size)[0]
                    if (Lambda[left_idx] <= err_tol).all():
                        continue
                    n_batch = left_idx.shape[0]
                    '''one is already added to the left charge in function get_target'''
                    target = cp.append(cp.zeros([n_batch, j], dtype='int32'), left_target[:, :size][left_idx], axis=1)
                    denominator = cp.sqrt(factorial(j)) * left_denominator[left_idx]
                    haf, haf_time, sigma_time = A_elem(Sigma, target, denominator, max_memory_in_gb)
                    tot_haf_time += haf_time
                    Gamma[0, cp.asnumpy(left_idx), j] = cp.asnumpy(haf / Z / Lambda[left_idx])

        elif compute_site == M - 1:

            num_pre = np.load(path + f'num_{compute_site - 1}.npy')
            num_pre = num_pre.reshape(num_pre.shape[0], -1)
            S_r = np.load(path + f'S_{compute_site - 1}.npy')
            right_target = get_target(num_pre)
            right_sum = cp.array(np.sum(num_pre, axis=1))
            right_denominator = cp.sqrt(cp.product(cp.array(factorial(num_pre)), axis=1))

            S_l = np.zeros((0, 0))
            U2, sq, U1 = get_U2_sq_U1(S_l, S_r)
            Z = np.sqrt(np.prod(np.cosh(sq)))
            Sigma = get_Sigma(U2, sq, U1)

            for j in np.arange(d):
                for size in np.arange(int(cp.nanmax(right_sum)) + 1):
                    right_idx = cp.where(right_sum == size)[0]
                    n_batch = right_idx.shape[0]
                    if size == 0 and j == 0:
                        Gamma[cp.asnumpy(right_idx), 0, j] = cp.asnumpy(cp.ones(n_batch) / Z)
                        continue

                    target = cp.copy(right_target[:, :size][right_idx])
                    if size == 0:
                        target = cp.zeros([n_batch, 0], dtype='int32')
                    target = cp.append(cp.zeros([n_batch, j], dtype=int), target, axis=1)
                    denominator = cp.sqrt(factorial(j)) * right_denominator[right_idx]
                    haf, haf_time, sigma_time = A_elem(Sigma, target, denominator, max_memory_in_gb)
                    Gamma[cp.asnumpy(right_idx), 0, j] = cp.asnumpy(haf / Z)

        else:
                    
            num_pre = np.load(path + f'num_{compute_site - 1}.npy')
            res_pre = np.load(path + f'res_{compute_site - 1}.npy')
            S_r = np.load(path + f'S_{compute_site - 1}.npy')
            right_target = cp.array(push_to_end(cp.asnumpy(get_target(num_pre))))
            right_sum = cp.array(np.sum(num_pre, axis=1))
            right_denominator = cp.sqrt(cp.product(cp.array(factorial(num_pre)), axis=1))

            num = np.load(path + f'num_{compute_site}.npy')
            res = np.load(path + f'res_{compute_site}.npy')
            S_l = np.load(path + f'S_{compute_site}.npy')
            num = num[res > err_tol]
            num = num.reshape(num.shape[0], -1)
            left_target = get_target(num)
            left_n_select = left_target.shape[1]
            left_sum = cp.array(np.sum(num, axis=1))
            full_sum = cp.repeat(left_sum.reshape(-1, 1), right_sum.shape[0], axis=1) + cp.repeat(right_sum.reshape(1, -1), left_sum.shape[0], axis=0)
            left_denominator = cp.sqrt(cp.product(cp.array(factorial(num)), axis=1))
            res = res[res > err_tol]
            U2, sq, U1 = get_U2_sq_U1(S_l, S_r) # S_l: left in equation, S_r : right in equation
            Sigma = get_Sigma(U2, sq, U1)
            Z = np.sqrt(np.prod(np.cosh(sq)))
            Lambda[:len(res)] = cp.array(np.sqrt(res))

            for j in np.arange(d):
                gpu_Gamma = cp.zeros([chi, chi], dtype='complex64')
                for size in np.arange(int(cp.nanmax(full_sum)) + 1):
                    left_idx, right_idx = cp.where(full_sum == size)
                    n_batch = left_idx.shape[0]
                    if (Lambda[left_idx] <= err_tol).all():
                        continue
                    if size == 0 and j == 0:
                        gpu_Gamma[right_idx, left_idx] = cp.ones(n_batch) / Z / Lambda[left_idx]
                        continue
                    target = cp.zeros([n_batch, size], dtype='int32')
                    target[:, :left_n_select] = cp.copy(left_target[:, :size][left_idx])
                    right_target_chosen = cp.copy(right_target[:, -size:][right_idx])
                    if size == 0:
                        right_target_chosen = cp.zeros([n_batch, 0], dtype='int32')
                    right_n_select = right_target_chosen.shape[1]
                    non_zero_locations = cp.where(right_target_chosen != 0)
                    right_target_chosen[non_zero_locations] += num.shape[1]
                    target[:, -right_n_select:] += right_target_chosen
                    target = cp.append(cp.zeros([n_batch, j], dtype=int), target, axis=1)
                    denominator = cp.sqrt(factorial(j)) * left_denominator[left_idx] * right_denominator[right_idx]
                    haf, haf_time, sigma_time = A_elem(Sigma, target, denominator, max_memory_in_gb)
                    tot_haf_time += haf_time
                    gpu_Gamma[right_idx, left_idx] = haf / Z / Lambda[left_idx]
                Gamma[:, :, j] = cp.asnumpy(gpu_Gamma)

        print('Total {}, haf {}, kron {}.'.format(time.time() - real_start, tot_haf_time, tot_kron_time))

        np.save(path + f"Gamma_{compute_site}.npy", Gamma)
        if compute_site < M - 1:
            np.save(path + f"Lambda_{compute_site}.npy", cp.asnumpy(Lambda))