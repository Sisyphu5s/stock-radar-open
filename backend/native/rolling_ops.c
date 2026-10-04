/* rolling_ops.c — Alpha/GP 滚动窗口算子原生内核（Apple Silicon / darwin）。
 *
 * 全部内核输入输出均为 double(float64) 一维数组，由调用方（native_ops.py，
 * ctypes 封装）负责分配：
 *   a    输入数组，可含 NaN（±inf 视为普通数值，参与运算，与 numpy 语义一致）
 *   n    数组长度（n >= 0）
 *   w    滚动窗口宽度
 *   out  输出缓冲，长度 n
 *
 * 边界与 NaN 语义：
 *   - 所有内核：w <= 0 或 w > n 时输出全 NaN；
 *     t < w-1（窗口未满）时输出 NaN。
 *   - rolling_sum / rolling_mean / rolling_max / rolling_min：
 *     窗口内出现 NaN → 该窗口结果 NaN（与 pandas rolling(min_periods=w) 一致）。
 *     sum/mean 用前缀和实现 O(n)；max/min 用单调队列实现 O(n)。
 *     （已知边界：同一窗口中 +inf 与 -inf 混加会产生 NaN 并随前缀和滞留，
 *       与逐窗口 numpy sum 的行为在“inf 跨窗口遗留”这一病态输入下不同；
 *       真实行情数据不含 inf，不影响 NaN 语义。）
 *   - rolling_rank：对窗口内有限元素排名，输出当前元素（窗口最新值）的
 *     0-1 归一化名次 (rank_1based - 1) / (k - 1)，k 为窗口内有限元素数；
 *     当前元素非有限或 k < 2 时输出 NaN —— 与 backend.ts_rank 逐位一致。
 *     O(n·w) 朴素实现。
 *   - rolling_product / rolling_skew / rolling_kurt / rolling_decay_linear：
 *     均 O(n·w) 朴素实现，公式与 backend.ts_product/ts_skew/ts_kurt/
 *     decay_linear 回退逐位一致（skew/kurt 为中心矩比，|v|<1e-12 归零；
 *     decay_linear 权重 w..1，分母 w(w+1)/2）；窗口含 NaN → 结果 NaN。
 *   - rolling_argmax / rolling_argmin：窗口内极值相对位置，从窗口尾向前
 *     计数（0=当前，w-1=最早）；NaN 语义同 np.argmax/np.argmin（首个 NaN
 *     恒胜出），与 backend.ts_argmax/ts_argmin 逐位一致。
 *
 * 返回：0 成功；1 参数非法（空指针 / n < 0）。
 */

#include <math.h>
#include <stdint.h>
#include <stdlib.h>

#define SR_OK 0
#define SR_ERR 1

static void fill_nan(double *out, int64_t n) {
    for (int64_t i = 0; i < n; i++) out[i] = NAN;
}

/* rolling_sum：前缀和 O(n)。窗口内 NaN → 结果 NaN（NaN 按 0 参与累加，
 * 另用前缀 NaN 计数判定窗口是否含 NaN，避免 cumsum 的 NaN 滞留）。 */
int rolling_sum(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    if (w == 1) { for (int64_t i = 0; i < n; i++) out[i] = a[i]; return SR_OK; }
    double *cs = (double *)malloc((size_t)n * sizeof(double));
    int64_t *pn = (int64_t *)malloc((size_t)n * sizeof(int64_t));
    if (cs == NULL || pn == NULL) { free(cs); free(pn); return SR_ERR; }
    for (int64_t i = 0; i < w - 1; i++) out[i] = NAN;
    for (int64_t t = 0; t < n; t++) {
        int is_nan = isnan(a[t]);
        cs[t] = (t > 0 ? cs[t - 1] : 0.0) + (is_nan ? 0.0 : a[t]);
        pn[t] = (t > 0 ? pn[t - 1] : 0) + (is_nan ? 1 : 0);
    }
    for (int64_t t = w - 1; t < n; t++) {
        double s = cs[t] - (t - w >= 0 ? cs[t - w] : 0.0);
        int64_t nwin = pn[t] - (t - w >= 0 ? pn[t - w] : 0);
        out[t] = (nwin > 0) ? NAN : s;
    }
    free(cs);
    free(pn);
    return SR_OK;
}

/* rolling_mean：前缀和 O(n)，窗口内 NaN → 结果 NaN。与
 * pandas rolling(w).mean()（min_periods=w）一致。 */
int rolling_mean(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    if (w == 1) { for (int64_t i = 0; i < n; i++) out[i] = a[i]; return SR_OK; }
    double *cs = (double *)malloc((size_t)n * sizeof(double));
    int64_t *pn = (int64_t *)malloc((size_t)n * sizeof(int64_t));
    if (cs == NULL || pn == NULL) { free(cs); free(pn); return SR_ERR; }
    for (int64_t i = 0; i < w - 1; i++) out[i] = NAN;
    for (int64_t t = 0; t < n; t++) {
        int is_nan = isnan(a[t]);
        cs[t] = (t > 0 ? cs[t - 1] : 0.0) + (is_nan ? 0.0 : a[t]);
        pn[t] = (t > 0 ? pn[t - 1] : 0) + (is_nan ? 1 : 0);
    }
    for (int64_t t = w - 1; t < n; t++) {
        double s = cs[t] - (t - w >= 0 ? cs[t - w] : 0.0);
        int64_t nwin = pn[t] - (t - w >= 0 ? pn[t - w] : 0);
        out[t] = (nwin > 0) ? NAN : s / (double)w;
    }
    free(cs);
    free(pn);
    return SR_OK;
}

/* rolling_max：单调递减队列 O(n)。NaN 不入队，另用 NaN 位置队列
 * 判定当前窗口是否含 NaN（NaN 移出窗口后恢复输出）。 */
int rolling_max(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    int64_t *dq = (int64_t *)malloc((size_t)n * sizeof(int64_t));
    int64_t *nq = (int64_t *)malloc((size_t)n * sizeof(int64_t));
    if (dq == NULL || nq == NULL) { free(dq); free(nq); return SR_ERR; }
    int64_t h = 0, tq = 0;   /* 值单调队列 [h, tq) */
    int64_t nh = 0, nt = 0;  /* NaN 位置队列 [nh, nt) */
    for (int64_t t = 0; t < n; t++) {
        int64_t wlo = t - w + 1;
        if (wlo > 0 && h < tq && dq[h] < wlo) h++;   /* 弹出窗口外的索引 */
        while (nh < nt && nq[nh] < wlo) nh++;        /* 弹出窗口外的 NaN */
        if (isnan(a[t])) {
            nq[nt++] = t;
        } else {
            while (tq > h && a[dq[tq - 1]] <= a[t]) tq--;
            dq[tq++] = t;
        }
        if (t < w - 1) {
            out[t] = NAN;
        } else {
            out[t] = (nh < nt) ? NAN : a[dq[h]];
        }
    }
    free(dq);
    free(nq);
    return SR_OK;
}

/* rolling_min：单调递增队列 O(n)，语义同 rolling_max。 */
int rolling_min(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    int64_t *dq = (int64_t *)malloc((size_t)n * sizeof(int64_t));
    int64_t *nq = (int64_t *)malloc((size_t)n * sizeof(int64_t));
    if (dq == NULL || nq == NULL) { free(dq); free(nq); return SR_ERR; }
    int64_t h = 0, tq = 0;
    int64_t nh = 0, nt = 0;
    for (int64_t t = 0; t < n; t++) {
        int64_t wlo = t - w + 1;
        if (wlo > 0 && h < tq && dq[h] < wlo) h++;
        while (nh < nt && nq[nh] < wlo) nh++;
        if (isnan(a[t])) {
            nq[nt++] = t;
        } else {
            while (tq > h && a[dq[tq - 1]] >= a[t]) tq--;
            dq[tq++] = t;
        }
        if (t < w - 1) {
            out[t] = NAN;
        } else {
            out[t] = (nh < nt) ? NAN : a[dq[h]];
        }
    }
    free(dq);
    free(nq);
    return SR_OK;
}

/* rolling_rank：O(n·w) 朴素实现，输出与 backend.ts_rank 逐位一致。
 * 有限元素计入有效数 k（±inf 不计）；计数 le 为窗口内“非 NaN 且 <= x”
 * 的元素数（与后端 win <= x 的逐元素比较一致：-inf 计入、+inf 不计、
 * NaN 恒 False），输出 (le-1)/(k-1)。 */
int rolling_rank(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    for (int64_t t = 0; t < n; t++) {
        if (t < w - 1) { out[t] = NAN; continue; }
        double x = a[t];
        if (!isfinite(x)) { out[t] = NAN; continue; }
        int64_t k = 0;   /* 窗口内有限元素数（含 x 自身） */
        int64_t le = 0;  /* 窗口内非 NaN 且 <= x 的元素数（含 x 自身与 -inf） */
        for (int64_t i = t - w + 1; i <= t; i++) {
            double v = a[i];
            if (!isnan(v)) {
                if (isfinite(v)) k++;
                if (v <= x) le++;
            }
        }
        out[t] = (k >= 2) ? (double)(le - 1) / (double)(k - 1) : NAN;
    }
    return SR_OK;
}

/* rolling_product：滚动连乘 O(n·w)。窗口内任一 NaN → 结果 NaN
 * （与 numpy prod / backend.ts_product 的 NaN 传播一致：NaN 恒胜出，
 * 不依赖其他元素）；±inf 与 0 混乘自然产生 NaN，无需特判。 */
int rolling_product(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    if (w == 1) { for (int64_t i = 0; i < n; i++) out[i] = a[i]; return SR_OK; }
    for (int64_t t = 0; t < n; t++) {
        if (t < w - 1) { out[t] = NAN; continue; }
        double p = 1.0;
        for (int64_t i = t - w + 1; i <= t; i++) {
            if (isnan(a[i])) { p = NAN; break; }
            p *= a[i];
        }
        out[t] = p;
    }
    return SR_OK;
}

/* rolling_skew：滚动偏度 O(n·w)，与 backend.ts_skew 回退公式逐位一致：
 *   m = 窗口均值；v = 二阶中心矩；s = 三阶中心矩（均除以 w，非样本无偏）；
 *   |v| < 1e-12 → 0；否则 s / (v^1.5 + 1e-12)。
 * 窗口含 NaN → 各矩为 NaN，最终输出 NaN（与 numpy where 分支语义一致）。 */
int rolling_skew(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    for (int64_t t = 0; t < n; t++) {
        if (t < w - 1) { out[t] = NAN; continue; }
        double m = 0.0;
        for (int64_t i = t - w + 1; i <= t; i++) m += a[i];
        m /= (double)w;
        double s2 = 0.0, s3 = 0.0;
        for (int64_t i = t - w + 1; i <= t; i++) {
            double d = a[i] - m;
            s2 += d * d;
            s3 += d * d * d;
        }
        double v = s2 / (double)w;
        double s = s3 / (double)w;
        out[t] = (fabs(v) < 1e-12) ? 0.0 : s / (pow(v, 1.5) + 1e-12);
    }
    return SR_OK;
}

/* rolling_kurt：滚动超额峰度 O(n·w)，与 backend.ts_kurt 回退公式逐位一致：
 *   m = 窗口均值；v = 二阶中心矩；q = 四阶中心矩；
 *   |v| < 1e-12 → 0；否则 q / (v^2 + 1e-12) - 3.0。窗口含 NaN → NaN。 */
int rolling_kurt(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    for (int64_t t = 0; t < n; t++) {
        if (t < w - 1) { out[t] = NAN; continue; }
        double m = 0.0;
        for (int64_t i = t - w + 1; i <= t; i++) m += a[i];
        m /= (double)w;
        double s2 = 0.0, s4 = 0.0;
        for (int64_t i = t - w + 1; i <= t; i++) {
            double d = a[i] - m;
            double d2 = d * d;
            s2 += d2;
            s4 += d2 * d2;
        }
        double v = s2 / (double)w;
        double q = s4 / (double)w;
        out[t] = (fabs(v) < 1e-12) ? 0.0 : q / (v * v + 1e-12) - 3.0;
    }
    return SR_OK;
}

/* rolling_argmax / rolling_argmin：滚动窗口内极值相对位置 O(n·w)。
 * 位置从窗口尾向前计数：0 = 当前（最新），w-1 = 窗口最早元素。
 * NaN 语义与 backend.ts_argmax/ts_argmin（np.argmax/np.argmin）一致：
 * 窗口含 NaN → 首个 NaN 的相对位置（numpy 中 NaN 恒胜出且首次出现者优先）；
 * 否则为首个极值（并列取最先出现）的相对位置。 */
int rolling_argmax(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    for (int64_t t = 0; t < n; t++) {
        if (t < w - 1) { out[t] = NAN; continue; }
        int64_t best = -1;   /* 窗口内最优值下标（绝对时间） */
        double bestv = 0.0;
        for (int64_t i = t - w + 1; i <= t; i++) {
            if (isnan(a[i])) { best = i; break; }   /* 首个 NaN 恒胜出 */
            if (best < 0 || a[i] > bestv) { best = i; bestv = a[i]; }
        }
        out[t] = (double)(t - best);   /* 从窗口尾向前计数，0=当前 */
    }
    return SR_OK;
}

int rolling_argmin(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    for (int64_t t = 0; t < n; t++) {
        if (t < w - 1) { out[t] = NAN; continue; }
        int64_t best = -1;
        double bestv = 0.0;
        for (int64_t i = t - w + 1; i <= t; i++) {
            if (isnan(a[i])) { best = i; break; }   /* 首个 NaN 恒胜出 */
            if (best < 0 || a[i] < bestv) { best = i; bestv = a[i]; }
        }
        out[t] = (double)(t - best);
    }
    return SR_OK;
}

/* rolling_decay_linear：线性衰减加权窗口均值 O(n·w)，与 backend.decay_linear
 * 逐位一致：权重从旧到新为 w, w-1, ..., 1，分母 w(w+1)/2；
 * 窗口含 NaN → 加权和 NaN → 结果 NaN。 */
int rolling_decay_linear(const double *a, int64_t n, int64_t w, double *out) {
    if (a == NULL || out == NULL || n < 0) return SR_ERR;
    if (n == 0 || w <= 0 || w > n) { fill_nan(out, n); return SR_OK; }
    if (w == 1) { for (int64_t i = 0; i < n; i++) out[i] = a[i]; return SR_OK; }
    double denom = (double)w * (double)(w + 1) / 2.0;
    for (int64_t t = 0; t < n; t++) {
        if (t < w - 1) { out[t] = NAN; continue; }
        double s = 0.0;
        for (int64_t i = t - w + 1; i <= t; i++) {
            s += a[i] * (double)(t - i + 1);   /* 权重 w, w-1, ..., 1 */
        }
        out[t] = s / denom;
    }
    return SR_OK;
}
