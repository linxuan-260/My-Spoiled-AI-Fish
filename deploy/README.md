# 部署

一条命令装完，再手动填一个 QQ 号就能跑。

## 前置

- Linux + systemd
- **AstrBot 已经装好**，且装在 venv 里（默认 `/opt/astrbot`，判断标准是 `/opt/astrbot/.venv/bin/astrbot` 存在）
- Python 3.12
- OneBot v11 后端（NapCat / SnowLuma 等）

完整版本矩阵见 [docs/依赖清单.md](../docs/依赖清单.md)。

## 安装

```bash
# 默认 /opt/astrbot
sudo bash deploy/install.sh

# 装到别的目录
sudo APP_DIR=/srv/astrbot bash deploy/install.sh
```

脚本做五件事，**不会启动服务，也不会覆盖已有配置**：

1. 把 `plugins/` 下两个插件拷到 `$APP_DIR/data/plugins/`（已存在则改名为 `.bak-install-<时间戳>`）
2. 把 `tools/` 拷到 `$APP_DIR/tools/`
3. 若配置不存在，写入 `config/fish_memory.config.example.json` 模板
4. 用 `$APP_DIR` 替换三个 systemd 单元的占位符并装到 `/etc/systemd/system/`
5. `systemctl enable` 三个服务

**可重复执行**：插件目录会被备份，配置永远不覆盖。

## 启动前必须做的

```bash
sudo vi /opt/astrbot/data/config/astrbot_plugin_fish_memory_config.json
```

把 `master.owner_id` 改成**你自己的 QQ 号**（字符串）。不填的后果：跨会话记忆全部失效，私聊里她不会记得你在群里说过的话。

然后：

```bash
sudo systemctl restart astrbot fishmem-panel fishmem-workers
```

## 验证

```bash
systemctl is-active astrbot fishmem-panel fishmem-workers
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:6186/
```

看四层是否真的注入了（这是唯一能证明配置生效的检查）：

```bash
/opt/astrbot/.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('/opt/astrbot/data/plugin_data/astrbot_plugin_fish_memory/memory/memory.db')
print(c.execute("select layer, count(*) from injection_ledger group by layer").fetchall())
PY
```

四层都有数字才算正常：

```
[('life', 274), ('memory', 405), ('mood', 863), ('style', 863)]
```

## 三个服务分别管什么

| 服务 | 作用 | 改它就得重启 |
|---|---|---|
| `astrbot` | 主进程，注入 / 检索 / 插件代码都在这里加载 | 改 `plugins/*.py` |
| `fishmem-workers` | ingest / life / digest 三进程 | 改 `worker/*.py` |
| `fishmem-panel` | 面板 + 星图（127.0.0.1:6186） | 改 `tools/fishmem_web.py` |

只改配置文件**什么都不用重启** —— 插件按 mtime 热重载。

## 面板与星图怎么看

两个都只绑 `127.0.0.1`，不要直接暴露公网：

```bash
ssh -N -L 16186:127.0.0.1:6186 -L 16185:127.0.0.1:6185 user@your-server
```

- 记忆面板：<http://127.0.0.1:16186>
- 记忆星图：<http://127.0.0.1:16186/starmap>

## 回滚

```bash
# 插件：装回脚本自动留的备份
cd /opt/astrbot/data/plugins
mv astrbot_plugin_fish_memory.bak-install-<时间戳> astrbot_plugin_fish_memory
systemctl restart astrbot

# systemd：直接删单元
rm /etc/systemd/system/fishmem-*.service
systemctl daemon-reload
```

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `缺少 /opt/astrbot/.venv/bin/python` | AstrBot 没装在 venv，或者路径不同 | 用 `APP_DIR=` 指对，或先按 AstrBot 文档建 venv |
| 面板起不来，`Address already in use` | 旧进程没退干净 | 等 40 秒，或 `pkill -f fishmem_web` 后重来 |
| 记忆一条都不入库 | worker 没起 | `journalctl -u fishmem-workers -n 50` |
| 四层只剩 `style` | 用了不带注入修复的旧版 | 确认 `markers.render()` 是一次性拼接，见 [架构](../docs/架构.md) |
| 面板亮「人格改动了」红灯 | `persona_guard.sha256` 还是旧基线 | 见 [配置 · 人格校验](../docs/配置.md) |

## 可选补丁

`patches/patch_imgfix.py` —— 仅在使用**智谱**多模态模型、且遇到 400「image format/parse error」时才需要。它会修改 AstrBot 的 `site-packages`：

```bash
sudo /opt/astrbot/.venv/bin/python deploy/patches/patch_imgfix.py
sudo systemctl restart astrbot
```

脚本会先备份被改动的文件。**升级 AstrBot 会覆盖这些改动，升级后要重跑。**
