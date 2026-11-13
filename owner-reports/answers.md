# Owner answers (2026-11-13)

| # | Answer |
|---|---|
| Q1 | HA Core **2026.11.1**, Supervisor **2026.11.0** |
| Q2 | L4T **R36.5.0** (JetPack 6.x line), GCID 43688277, board `generic`, aarch64, kernel variant `oot`, built 2026-01-16 |
| Q3 | First rooms: **Bedroom** and **Livingroom**. Entities: take them from the area registry (not listed by hand) |
| Q4 | **No.** No perimeter devices hidden on `switch`/oddly named entities |
| Q5 | **Yes.** Exports in `owner-reports/nodered/` (`bedroom.json`, `livingroom.json`) |
| Q6 | Cloud model **`claude-haiku-5-5`** is enough. Once local Qwen on the Jetson works, the local model becomes the default and the cloud is the fallback. Budget: not specified; keep €1/day |
| Q7 | Jetson: **`192.168.1.215`** |

Raw `/etc/nv_tegra_release`:

```
# R36 (release), REVISION: 5.0, GCID: 43688277, BOARD: generic, EABI: aarch64, DATE: Fri Jan 16 03:50:45 UTC 2026
# KERNEL_VARIANT: oot
TARGET_USERSPACE_LIB_DIR=nvidia
TARGET_USERSPACE_LIB_DIR_PATH=usr/lib/aarch64-linux-gnu/nvidia
```
