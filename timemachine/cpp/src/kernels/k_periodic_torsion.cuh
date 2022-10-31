#include "../fixed_point.hpp"
#include "k_fixed_point.cuh"

template <typename RealType> inline __device__ RealType dot_product(const RealType a[3], const RealType b[3]) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

__device__ float rmul_rn(float a, float b) { return __fmul_rn(a, b); }

__device__ double rmul_rn(double a, double b) { return __dmul_rn(a, b); }

template <typename RealType>
inline __device__ void cross_product(const RealType a[3], const RealType b[3], RealType c[3]) {
    // these extra __dmul_rn calls are needed to preserve bitwise anticommutativity
    // i.e. cross(a,b) is bitwise identical to -cross(b,a) except in the sign-bit
    c[0] = rmul_rn(a[1], b[2]) - rmul_rn(a[2], b[1]);
    c[1] = rmul_rn(a[2], b[0]) - rmul_rn(a[0], b[2]);
    c[2] = rmul_rn(a[0], b[1]) - rmul_rn(a[1], b[0]);
}

template <typename RealType, int D>
void __global__ k_periodic_torsion(
    const int T,                          // number of bonds
    const double *__restrict__ coords,    // [n, 3]
    const double *__restrict__ params,    // [p, 3]
    const int *__restrict__ torsion_idxs, // [b, 4]
    unsigned long long *__restrict__ du_dx,
    unsigned long long *__restrict__ du_dp,
    unsigned long long *__restrict__ u) {

    const auto t_idx = blockDim.x * blockIdx.x + threadIdx.x;

    if (t_idx >= T) {
        return;
    }

    int i_idx = torsion_idxs[t_idx * 4 + 0];
    int j_idx = torsion_idxs[t_idx * 4 + 1];
    int k_idx = torsion_idxs[t_idx * 4 + 2];
    int l_idx = torsion_idxs[t_idx * 4 + 3];

    RealType rij[3];
    RealType rkj[3];
    RealType rkl[3];

    RealType rkj_norm_square = 0;

    // (todo) cap to three dims, while keeping stride at 4
    for (int d = 0; d < 3; d++) {
        RealType vij = coords[j_idx * D + d] - coords[i_idx * D + d];
        RealType vkj = coords[j_idx * D + d] - coords[k_idx * D + d];
        RealType vkl = coords[l_idx * D + d] - coords[k_idx * D + d];
        rij[d] = vij;
        rkj[d] = vkj;
        rkl[d] = vkl;
        rkj_norm_square += vkj * vkj;
    }

    RealType rkj_norm = sqrt(rkj_norm_square);
    RealType n1[3], n2[3];

    cross_product(rij, rkj, n1);
    cross_product(rkj, rkl, n2);

    RealType n1_norm_square, n2_norm_square;

    n1_norm_square = dot_product(n1, n1);
    n2_norm_square = dot_product(n2, n2);

    RealType n3[3];
    cross_product(n1, n2, n3);

    RealType d_angle_dR0[3];
    RealType d_angle_dR3[3];
    RealType d_angle_dR1[3];
    RealType d_angle_dR2[3];

    RealType rij_dot_rkj = dot_product(rij, rkj);
    RealType rkl_dot_rkj = dot_product(rkl, rkj);

    for (int d = 0; d < 3; d++) {
        d_angle_dR0[d] = rkj_norm / n1_norm_square * n1[d];
        d_angle_dR3[d] = -rkj_norm / n2_norm_square * n2[d];
        d_angle_dR1[d] =
            (rij_dot_rkj / rkj_norm_square - 1) * d_angle_dR0[d] - d_angle_dR3[d] * rkl_dot_rkj / rkj_norm_square;
        d_angle_dR2[d] =
            (rkl_dot_rkj / rkj_norm_square - 1) * d_angle_dR3[d] - d_angle_dR0[d] * rij_dot_rkj / rkj_norm_square;
    }

    RealType rkj_n = sqrt(dot_product(rkj, rkj));

    for (int d = 0; d < 3; d++) {
        rkj[d] /= rkj_n;
    }

    RealType y = dot_product(n3, rkj);
    RealType x = dot_product(n1, n2);
    RealType angle = atan2(y, x);

    int kt_idx = t_idx * 3 + 0;
    int phase_idx = t_idx * 3 + 1;
    int period_idx = t_idx * 3 + 2;

    RealType kt = params[kt_idx];
    RealType phase = params[phase_idx];
    RealType period = params[period_idx];

    RealType prefactor = kt * sin(period * angle - phase) * period;

    if (du_dx) {
        for (int d = 0; d < 3; d++) {
            atomicAdd(du_dx + i_idx * D + d, FLOAT_TO_FIXED_BONDED<RealType>(d_angle_dR0[d] * prefactor));
            atomicAdd(du_dx + j_idx * D + d, FLOAT_TO_FIXED_BONDED<RealType>(d_angle_dR1[d] * prefactor));
            atomicAdd(du_dx + k_idx * D + d, FLOAT_TO_FIXED_BONDED<RealType>(d_angle_dR2[d] * prefactor));
            atomicAdd(du_dx + l_idx * D + d, FLOAT_TO_FIXED_BONDED<RealType>(d_angle_dR3[d] * prefactor));
        }
    }

    if (du_dp) {
        RealType du_dkt = 1 + cos(period * angle - phase);
        RealType du_dphase = kt * sin(period * angle - phase);
        RealType du_dperiod = -kt * sin(period * angle - phase) * angle;

        atomicAdd(du_dp + kt_idx, FLOAT_TO_FIXED_BONDED(du_dkt));
        atomicAdd(du_dp + phase_idx, FLOAT_TO_FIXED_BONDED(du_dphase));
        atomicAdd(du_dp + period_idx, FLOAT_TO_FIXED_BONDED(du_dperiod));
    }

    if (u) {
        atomicAdd(u + i_idx, FLOAT_TO_FIXED_BONDED(kt * (1 + cos(period * angle - phase))));
    }
}
