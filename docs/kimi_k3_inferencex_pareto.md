# Kimi K3 InferenceX Pareto

两张图对比 Kimi K3 2.8T 的每卡吞吐和交互性。除 ATOMesh 以外的曲线来自 SemiAnalysis InferenceX CSV。ATOMesh 的点是本地跑数，手写在脚本里，两条曲线共用同一组点。

| 文件 | 作用 |
| --- | --- |
| `plot_inferencex_kimi_k3_pareto.py` | 画 PNG。ATOMesh 在 `ATOMESH_ROWS`，单独的 PP8 五角星在 `PP_STAR` |
| `InferenceX_Kimi-K3_e2e (1).csv` | E2E 横轴：`P90 E2E Normalized Interactivity (tok/s/user)` |
| `InferenceX_Kimi-K3_interactivity (1).csv` | Interactivity 横轴：`P90 Interactivity (tok/s/user)` |
| `kimi-k3-inferencex-pareto.canvas.tsx` | Canvas 副本，点必须和脚本前沿一致 |
| `InferenceX_Kimi-K3_e2e_pareto.png` | E2E 图 |
| `InferenceX_Kimi-K3_interactivity_pareto.png` | Interactivity 图 |

纵轴都是 `Token Throughput per Chip (tok/s/chip)`。H200 在 CSV 里，但前沿低于约 264 tok/s/chip，脚本不画。

重画：

```bash
python plot_inferencex_kimi_k3_pareto.py
```

依赖 matplotlib、numpy、scipy。终端会打印每条曲线的 Pareto 前沿。

## 加一个 ATOMesh 点

在 `ATOMESH_ROWS` 加一行。ISL/OSL 固定为 1048576/1024。

```python
{"Topology": "1P1D-DS3", "Concurrency": "88", "e2e": 5.81, "itl": 28.09, "y": 14414.87},
```

- `e2e`：P90 E2E Normalized Interactivity
- `itl`：P90 Interactivity，等于 `1000 / TPOT p90 (ms)`
- `y`：Total tok/s/GPU

前沿只按 `itl` 选一次。E2E 图用同一批点的 `e2e` 做横轴，不要再按 E2E 做一次 Pareto。函数名保持 `atomesh_chart_rows(use_e2e_x: bool)`。标签太挤时改 `label_offset()` 里对应标签的偏移。

不进紫色曲线、只标一个记号的点改 `PP_STAR`。现在的蓝星是 `1P1D-PP8-DCP8-TP1-TP8-DSPARK3-PART12` C88：e2e 11.14，itl 27.68，y 15604.35。

## 更新别的硬件

换掉两份 CSV，不要改 `ATOMESH_ROWS`。新的 Hardware Key 要加进 `SHARED_SERIES`，否则不会画。换完 CSV 后把 Canvas 里对应系列的点改成脚本打印出的前沿。

## 当前 ATOMesh 点

| Topology | Concurrency | e2e | itl | tok/s/GPU |
| --- | ---: | ---: | ---: | ---: |
| 1P1D-DSPARK7 | 1 | 27.26 | 156.25 | 694.19 |
| 1P1D-DSPARK7 | 4 | 31.76 | 147.49 | 1364.65 |
| 1P2D-DS3 | 48 | 26.42 | 69.42 | 9703.69 |
| 1P1D-DS3 | 48 | 24.78 | 48.95 | 12104.43 |
| 1P1D-DS3 | 56 | 17.41 | 40.91 | 12569.33 |
| 1P1D-DS3 | 72 | 10.39 | 33.41 | 14152.96 |
| 1P1D-DS3 | 88 | 5.81 | 28.09 | 14414.87 |

`1P1D-DS3` 是 `1P1D-TP8-DCP8-DSPARK3`，`1P2D-DS3` 是 `1P2D-TP8-DCP8-DSPARK3`。`1P1D-DS3 C16` 已从曲线删除。InferenceX CSV 日期是 2026-10-15。
