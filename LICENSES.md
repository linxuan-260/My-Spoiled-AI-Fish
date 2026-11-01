# 许可证说明

本仓库是**混合许可**，按目录区分。

| 路径 | 许可证 | 来源 |
|---|---|---|
| `plugins/astrbot_plugin_fish_memory/` | **MIT**（见根目录 `LICENSE`） | 本项目自研 |
| `tools/`、`docs/`、`config/`、`scripts/` | **MIT** | 本项目自研 |
| `plugins/astrbot_plugin_qzone/` | **GPL-3.0**（见该目录内 `LICENSE`） | 上游 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) v3.1.3 的修改版 |

## 关于 qzone 部分

`plugins/astrbot_plugin_qzone` 是第三方 GPL-3.0 代码的修改版本，因此：

- 该目录整体继续受 **GPL-3.0** 约束，不能改为 MIT
- 保留了上游的 `LICENSE`、`metadata.yaml` 中的作者署名与仓库地址
- 本地修改内容记录在 `CHANGELOG.md` 和该目录的 `CHANGELOG.md` 中
- 再分发时请同样保留 GPL-3.0 许可与署名

## 关于 MIT 部分

`plugins/astrbot_plugin_fish_memory` 及 `tools/` 等自研部分采用 MIT，可自由用于商业项目、可闭源再分发。

两部分的组合方式是两个独立插件目录，各自独立运行、通过 AstrBot 的插件接口交互，不构成单一衍生作品。
