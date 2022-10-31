#pragma once

#include "potential.hpp"
#include <vector>

namespace timemachine {

template <typename RealType, bool Negated> class NonbondedPairList : public Potential {

private:
    const int M_; // number of pairs

    double beta_;
    double cutoff_;

    int *d_pair_idxs_; // [M, 2]
    double *d_scales_; // [M, 2]

public:
    NonbondedPairList(
        const std::vector<int> &pair_idxs, // [M, 2]
        const std::vector<double> &scales, // [M, 2]
        const double beta,
        const double cutoff);

    ~NonbondedPairList();

    virtual void execute_device(
        const int N,
        const int P,
        const double *d_x,
        const double *d_p,
        const double *d_box,
        unsigned long long *d_du_dx,
        unsigned long long *d_du_dp,
        unsigned long long *d_u,
        cudaStream_t stream) override;

    void du_dp_fixed_to_float(const int N, const int P, const unsigned long long *du_dp, double *du_dp_float) override;
};

} // namespace timemachine
