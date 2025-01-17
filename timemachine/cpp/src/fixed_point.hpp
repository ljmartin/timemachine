#pragma once

#define FIXED_EXPONENT 0x1000000000

// we need to use a different level of precision for parameter derivatives
#define FIXED_EXPONENT_DU_DCHARGE 0x1000000000
#define FIXED_EXPONENT_DU_DSIG 0x2000000000
#define FIXED_EXPONENT_DU_DEPS 0x4000000000 // this is just getting silly
#define FIXED_EXPONENT_DU_DW 0x1000000000

template <typename RealType, unsigned long long EXPONENT>
RealType __host__ __device__ __forceinline__ FIXED_TO_FLOAT_DU_DP(unsigned long long v) {
    return static_cast<RealType>(static_cast<long long>(v)) / EXPONENT;
}

template <typename RealType> RealType __host__ __device__ __forceinline__ FIXED_TO_FLOAT(unsigned long long v) {
    return static_cast<RealType>(static_cast<long long>(v)) / FIXED_EXPONENT;
}

// FIXED_ENERGY_TO_FLOAT should be paired with a `fixed_point_overflow` as if it is beyond the long long representation
// the value returned will be meaningless
template <typename RealType> RealType __host__ __device__ __forceinline__ FIXED_ENERGY_TO_FLOAT(__int128 v) {
    return static_cast<RealType>(static_cast<long long>(v)) / FIXED_EXPONENT;
}

// fixed_point_overflow detects if a __int128 fixed point representation is 'overflowed'
// which means is outside of the long long range of representation.
bool __host__ __device__ __forceinline__ fixed_point_overflow(__int128 val) {
    __int128 max = LLONG_MAX;
    __int128 min = LLONG_MIN;
    return val >= max || val <= min;
}
