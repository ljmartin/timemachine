#include "../fixed_point.hpp"
#include "k_fixed_point.cuh"
#include "k_flat_bottom_bond.cuh"

template <typename RealType> RealType __device__ __forceinline__ stable_log_1_exp_neg(RealType x) {
    const RealType LOG_2 = 0.693147180559945309417232121;
    return x < LOG_2 ? log(-expm1(-x)) : log1p(-exp(-x));
}

template <typename RealType>
void __global__ k_log_flat_bottom_bond(
    const int B, // number of bonds
    const double *__restrict__ coords,
    const double *__restrict__ box,
    const double *__restrict__ params, // [B, 3]
    const int *__restrict__ bond_idxs, // [B, 2]
    const double beta,
    unsigned long long *__restrict__ du_dx,
    unsigned long long *__restrict__ du_dp,
    __int128 *__restrict__ u) {

    // which bond
    const auto b_idx = blockDim.x * blockIdx.x + threadIdx.x;
    if (b_idx >= B) {
        return;
    }

    // which atoms
    const int num_atoms = 2;
    int atoms_idx = b_idx * num_atoms;
    int src_idx = bond_idxs[atoms_idx + 0];
    int dst_idx = bond_idxs[atoms_idx + 1];

    // look up params
    const int num_params = 3;
    int params_idx = b_idx * num_params;
    int k_idx = params_idx + 0;
    int rmin_idx = params_idx + 1;
    int rmax_idx = params_idx + 2;

    RealType k = params[k_idx];
    RealType rmin = params[rmin_idx];
    RealType rmax = params[rmax_idx];

    // compute common subexpressions involving distance, displacements
    RealType dx[3];
    RealType r2 = 0;
    for (int d = 0; d < 3; d++) {
        double delta = coords[src_idx * 3 + d] - coords[dst_idx * 3 + d];
        delta -= box[d * 3 + d] * nearbyint(delta / box[d * 3 + d]);
        dx[d] = delta;
        r2 += delta * delta;
    }
    RealType r = sqrt(r2);

    // branches -> masks
    RealType r_gt_rmax = static_cast<RealType>(r > rmax);
    RealType r_lt_rmin = static_cast<RealType>(r < rmin);

    RealType nrg = compute_flat_bottom_energy(k, r, rmin, rmax);

    if (u) {
        // RealType u_real = -log(1 - exp(-beta * nrg)) / beta;
        RealType u_real = -stable_log_1_exp_neg(beta * nrg) / beta;
        // Store energy in buffer
        u[b_idx] = FLOAT_TO_FIXED_ENERGY<RealType>(u_real);
    }

    RealType prefactor = -exp(-beta * nrg) / (1 - exp(-beta * nrg));

    if (du_dp) {
        // compute parameter derivatives
        RealType du_dk_real = (r_gt_rmax * (pow(r - rmax, 4) / 4)) + (r_lt_rmin * (pow(r - rmin, 4) / 4));
        RealType du_drmin_real = r_lt_rmin * (-k * pow(r - rmin, 3));
        RealType du_drmax_real = r_gt_rmax * (-k * pow(r - rmax, 3));

        // cast float -> fixed
        auto du_dk = FLOAT_TO_FIXED_BONDED<RealType>(du_dk_real * prefactor);
        auto du_drmin = FLOAT_TO_FIXED_BONDED<RealType>(du_drmin_real * prefactor);
        auto du_drmax = FLOAT_TO_FIXED_BONDED<RealType>(du_drmax_real * prefactor);

        // increment du_dp array
        atomicAdd(du_dp + k_idx, du_dk);
        atomicAdd(du_dp + rmin_idx, du_drmin);
        atomicAdd(du_dp + rmax_idx, du_drmax);
    }

    if (du_dx) {
        RealType du_dr = k * ((r_gt_rmax * pow(r - rmax, 3)) + (r_lt_rmin * pow(r - rmin, 3)));

        for (int d = 0; d < 3; d++) {
            // compute du/dcoords
            RealType du_dsrc_real = du_dr * dx[d] / r;
            RealType du_ddst_real = -du_dsrc_real;

            // cast float -> fixed
            auto du_dsrc = FLOAT_TO_FIXED_BONDED<RealType>(prefactor * du_dsrc_real);
            auto du_ddst = FLOAT_TO_FIXED_BONDED<RealType>(prefactor * du_ddst_real);

            // increment du_dx array
            atomicAdd(du_dx + src_idx * 3 + d, du_dsrc);
            atomicAdd(du_dx + dst_idx * 3 + d, du_ddst);
        }
    }
}
