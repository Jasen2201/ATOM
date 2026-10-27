// Memory bandwidth (STREAM-style triad, OpenMP) and latency (pointer chase) for node_check.sh.
//   membw bw   <MiB per array> <repeats>  -> best triad GB/s
//   membw lat  <MiB buffer>    <loads>    -> ns per dependent load
//   membw load <seconds>                  -> triad until the time is up (background load)
// Build: gcc -O2 -fopenmp -o membw membw.c   (without -fopenmp it runs single-threaded)
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _OPENMP
#include <omp.h>
#endif

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

static double *alloc_init(size_t n, double v) {
    double *p = aligned_alloc(64, n * sizeof(double));
    if (!p) { perror("aligned_alloc"); exit(1); }
#pragma omp parallel for schedule(static)
    for (size_t i = 0; i < n; i++) p[i] = v;  // first touch by the worker threads
    return p;
}

static double triad(double *a, const double *b, const double *c, size_t n) {
    double t = now();
#pragma omp parallel for schedule(static)
    for (size_t i = 0; i < n; i++) a[i] = b[i] + 3.0 * c[i];
    return now() - t;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: membw bw <MiB> <repeats> | lat <MiB> <loads> | load <seconds>\n");
        return 2;
    }
    if (!strcmp(argv[1], "bw") || !strcmp(argv[1], "load")) {
        int is_load = !strcmp(argv[1], "load");
        size_t mib = is_load ? 512 : strtoull(argv[2], 0, 10);
        size_t n = mib * 1024 * 1024 / sizeof(double);
        double *a = alloc_init(n, 0), *b = alloc_init(n, 1), *c = alloc_init(n, 2);
        if (is_load) {
            double end = now() + atof(argv[2]);
            while (now() < end) triad(a, b, c, n);
            return 0;
        }
        int reps = atoi(argv[3]);
        double best = 1e30;
        for (int r = 0; r < reps; r++) {
            double t = triad(a, b, c, n);
            if (r > 0 && t < best) best = t;  // first pass warms up
        }
        int threads = 1;
#ifdef _OPENMP
        threads = omp_get_max_threads();
#endif
        printf("triad_gbps %.1f threads %d check %.1f\n", 3.0 * n * sizeof(double) / best / 1e9, threads, a[n / 2]);
        return 0;
    }
    if (!strcmp(argv[1], "lat")) {
        size_t bytes = strtoull(argv[2], 0, 10) * 1024 * 1024;
        size_t loads = argc > 3 ? strtoull(argv[3], 0, 10) : 20000000;
        size_t stride = 64 / sizeof(size_t), lines = bytes / 64;
        size_t *buf = aligned_alloc(64, lines * 64);
        size_t *order = malloc(lines * sizeof(size_t));
        if (!buf || !order) { perror("alloc"); return 1; }
        for (size_t i = 0; i < lines; i++) order[i] = i;
        srand(12345);
        for (size_t i = lines - 1; i > 0; i--) {  // random cyclic order defeats the prefetchers
            size_t j = ((size_t)rand() * RAND_MAX + rand()) % (i + 1), t = order[i];
            order[i] = order[j];
            order[j] = t;
        }
        for (size_t i = 0; i < lines; i++) buf[order[i] * stride] = order[(i + 1) % lines] * stride;
        size_t p = 0;
        for (size_t i = 0; i < lines; i++) p = buf[p];  // warm the TLB / page tables
        double t = now();
        for (size_t i = 0; i < loads; i++) p = buf[p];
        t = now() - t;
        printf("lat_ns %.1f check %zu\n", t / loads * 1e9, p);
        return 0;
    }
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 2;
}
