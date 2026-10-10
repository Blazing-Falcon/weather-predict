# Storage Retention and Budget

Dev snapshot from `nowcast disk` on 2026-11-19. Sizes use decimal MB and GB. A blank projection means there is no measured rate for that cap. The nested BMKG dead-letter queue has its own row and window.

| Dataset | Window | Dev Size (MB) | Projected Size at Cap | Enforcement |
| --- | --- | ---: | ---: | --- |
| `bronze/bmkg` | 12 months | Absent | | Retention |
| `bronze/bmkg/_dlq` | 3 months | Absent | | Retention |
| `bronze/imerg` | 7 days | 0.783 | | Retention and inline backfill cleanup |
| `bronze/mergir` | 7 days | 0.000 | | Retention and inline backfill cleanup |
| `bronze/himawari` | 6 hours | 51.500 | | Retention and inline cleanup after processing |
| `bronze/gauges` | 7 days | 0.069 | | Retention and inline GHCNh cleanup after publishing |
| `silver/rain_kabkota` | 10 years | 1.741 | 0.20 GB at 1.7 MB/month | Retention |
| `silver/cloud_kabkota` | 10 years | 3.813 | 0.46 GB at 3.8 MB/month for MERGIR | Retention |
| `silver/bmkg_forecast` | 3 years | Absent | | Retention |
| `silver/gauge_obs` | 10 years | 0.069 | | Retention |
| `gold/predictions` | 12 months | Absent | | Retention |
| `gold/verification` | 12 months | Absent | | Retention |
| `gold/scores_daily` | 10 years | Absent | | Retention |
| `gold/map_frames` | 7 days | Absent | | Retention |
| `gold/tiles/rain` | 30 days | Absent | | Retention |
| `gold/tiles/cloud` | 7 days | Absent | | Retention |
| `models` | 5 versions | 0.000 | | Retention by folder modification time |
| `dagster_runs` | 30 days | Absent | | Dagster cleanup job |
| `dagster_compute_logs` | 30 days | Absent | | Dagster cleanup job |
| `ref` | | 160.144 | | Disk check |
| `state` | | 41.540 | | Disk check |
| Other | | 0.000 | | Disk check |

`nowcast retention` removes expired partitions. It uses file modification times for unpartitioned files and partitions wider than their window. Use `nowcast retention --dry-run` to preview deletions.

`nowcast disk` measured 0.259659 GB against the 8 GB budget. It excludes `tmp/` and returns exit code 1 above the budget. The full-resolution boundaries alone use 159 MB, above the 0.1 GB planned for all of `ref/`.
