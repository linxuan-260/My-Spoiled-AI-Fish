# My-Spoiled-AI-Fish

> 一个「会过日子」的 QQ 机器人人格系统：五层记忆、只读人格、可逆注入。

**My-Spoiled-AI-Fish**（肥鱼）不是又一个聊天机器人框架，而是一套**不改变人格、只增强上下文**的记忆中间件。它挂在 AstrBot 的 `on_llm_request` 上，每次调用模型前把「她记得什么、心情怎样、正在干嘛、说话什么样」拼进 system prompt，调用后原样还原。

核心理念：**人格是只读的**。所有增强都是带 marker 的临时注入块，拿掉 marker 后 prompt 与注入前逐字节相同。

> ⚠️ **警告**：请在使用前，将 Prompt 中的 QQ 替换为你自己的 QQ 号！否则你的 AI 女儿不仅不会理你，还会叫你‘滚’，并质问你是谁！

---

## 特性

- **五层记忆**：记忆 / 情绪 / 生活 / 表达 / 反馈，各自独立打分为预算
- **人格只读**：从不写回 `personas` 表，可逆、可审计
- **跨会话记忆**：主人在私聊里说过的事，群里也记得
- **共享层**：群里的事可以定向开放给指定会话，陌生人读不到
- **自主探索**：每两小时自己搜个好奇的问题、读一段正文、用她自己的话记下来
- **记忆星图**：把记忆的归属、共注入、语义关系画成一张图
- **注入账本**：每次注入的层、命中的记忆 id、字符数全部落库，事后可查

---

## 快速开始

```bash
# 1. 一条命令装完（插件 + tools + systemd 单元）
sudo bash deploy/install.sh

# 2. 配身份（必改）
sudo vi /opt/astrbot/data/config/astrbot_plugin_fish_memory_config.json
#   master.owner_id  ->  你自己的 QQ

# 3. 启动
sudo systemctl restart astrbot fishmem-panel fishmem-workers
```

完整步骤见 **[docs/安装.md](docs/安装.md)**，版本矩阵见 **[docs/依赖清单.md](docs/依赖清单.md)**。

---

## 部署

`deploy/` 让这个仓库从「一份代码」变成「能跑起来的一套服务」：

| 文件 | 作用 |
|---|---|
| `install.sh` | 拷插件 + tools、写配置模板、装三个 systemd 单元 |
| `systemd/*.service` | astrbot / fishmem-panel / fishmem-workers |
| `requirements.txt` | 额外依赖（只有 qzone 需要） |
| `patches/patch_imgfix.py` | 可选：智谱多模态 400 的修复 |

```bash
sudo bash deploy/install.sh                        # 默认 /opt/astrbot
sudo APP_DIR=/srv/astrbot bash deploy/install.sh   # 装到别的目录
```

三个服务的分工：

| 服务 | 管什么 | 改什么要重启 |
|---|---|---|
| `astrbot` | 主进程，注入 / 检索都在这里加载 | `plugins/*.py` |
| `fishmem-workers` | ingest / life / digest 三进程 | `worker/*.py` |
| `fishmem-panel` | 面板 + 星图（仅 127.0.0.1:6186） | `tools/fishmem_web.py` |

**只改配置不用重启任何服务** —— 插件按文件 mtime 热重载。

### 脚本的保守设计

- **不覆盖已有配置**：配置已存在就跳过，不会冲掉你填的 `owner_id`
- **不删除旧插件**：已存在的插件目录会改名成 `.bak-install-<时间戳>`
- **只 `enable` 不 `start`**：启动时机交给你，装完不会顺手重启线上
- **可重复执行**：装坏了再跑一遍即可

第一次用建议先读一遍脚本 —— 它只做拷贝、模板替换和单元安装，不碰数据库目录。

### 依赖

五层记忆插件**零第三方依赖**（纯标准库）；qzone 需要 `Pillow` / `json5` / `apilmoji`。完整版本矩阵（AstrBot 4.29.3 / Python 3.12.16 / 19 个插件的精确版本）见 [docs/依赖清单.md](docs/依赖清单.md)。

三个 systemd 单元的内容取自真实运行环境，包括 `RestartSec`、`TimeoutStopSec`、`KillMode=control-group` 这些容易漏的细节。

---

## 身份绑定规则 ⭐

这是整个系统最容易配错、后果也最严重的一块。搞错了要么「她记不住你说的话」，要么「陌生人能读到群里的内容」。

### 三个 QQ 号，别搞混

| 角色 | 是什么 | 填在哪 |
|---|---|---|
| **机器人 QQ** | 跑着 OneBot 的那个号 | 不填，从会话的 `umo` 里自动解析 |
| **主人 QQ** | 你自己 | `master.owner_id`，字符串 |
| **群号** | 需要共享的那个群 | 写进 `public_scopes` 的会话字符串里 |

### 会话标识（umo）长什么样

```
aiocqhttp-<机器人QQ>:GroupMessage:<群号>
aiocqhttp-<机器人QQ>:FriendMessage:<对方QQ>
```

`public_scopes` 和 `public_readers` 里填的就是这种整串。

### 可见性三层

| 值 | 谁看得到 | 什么时候自动打上 |
|---|---|---|
| `private` | 只有本会话 | 默认 |
| `owner` | 本会话 + 主人的**其它任何会话** | 一批消息只来自 `owner_id` |
| `public` | 共享层读者 | 手动公开，或 `auto_share_categories` 命中 |

同一条内容的哈希撞车时，可见性**只升不降** —— 不会因为一次误判把已共享的记忆降回私有。

### 典型配置：群里的事，私聊里也想得起来

```json
{
  "master": {
    "owner_id": "123456789",
    "public_scopes":  ["aiocqhttp-<机器人QQ>:GroupMessage:<群号>"],
    "public_readers": ["aiocqhttp-<机器人QQ>:FriendMessage:123456789"]
  }
}
```

含义：**来源**是那个群，**读者**是主人私聊。`public_readers` 留空 = 所有会话都能读来源 —— 除非你确定，否则不要留空。

### 怎么验证绑定生效了

```bash
# 私聊她问一件群里说过的事，然后看这次注入命中了哪些记忆
/opt/astrbot/.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('/opt/astrbot/data/plugin_data/astrbot_plugin_fish_memory/memory/memory.db')
for row in c.execute("select ts,layer,memory_ids from injection_ledger order by id desc limit 5"):
    print(row)
PY
```

如果私聊场景下 `layer='memory'` 的 `memory_ids` 里出现了群里的记忆 id，说明共享层通了。

反过来，用**陌生号**私聊她，同样的查询应该一条 `memory` 都没有。

### 常见配错的症状

| 症状 | 原因 |
|---|---|
| 私聊里她不记得你刚才说的 | `owner_id` 没填或填错 |
| 陌生人能聊到群里的内容 | `public_readers` 留空（=对所有会话开放） |
| 群里的事私聊问不出来 | `public_scopes` 里的 umo 拼错了，注意中间是 `-` 和 `:` |
| 记忆全挤在一个会话下 | 入库批次跨会话混在一起（本仓库已按 `scope_id` 分组修复） |

---

## 群聊防禁言指南 ⭐

被禁言基本只有两种原因：**平台风控**（发得太勤）和**群管不满**（刷屏、乱跑自动行为）。

**三条硬规则：**

1. **`trigger.read_prob` 保持 0.001 或更小。** 它决定「每来一条消息就随机去翻对方空间」的概率，调大等于让机器人到处翻别人说说。
2. **说说的两条间隔 ≥ 3 小时、每天 ≤ 6 条。** 这层闸在代码里（`service.publish_post`），改配置绕不过去。
3. **定时任务必须带抖动。** `publish_offset = 600` 让实际执行散在 20 分钟窗口里；设成 0 就成了「每天 23:30:00 整」的机器人签名。

另外：`send_read_card` **保持 `false`**。开了以后，读到的好友说说不论是否「好友可见」都会被渲染成卡片发出去 —— 这是隐私事故，也是被投诉的高发点。

完整清单（含忽略名单、权限边界、被禁言后的处置）见 **[docs/防禁言.md](docs/防禁言.md)**。

---

## 目录结构

```
plugins/
  astrbot_plugin_fish_memory/   五层记忆插件（本项目主体，MIT）
  astrbot_plugin_qzone/         QQ空间插件（上游第三方，GPL-3.0）
tools/
  fishmem_web.py                五层记忆面板 + /api/graph
  fishmem_web_ui.html           面板前端
  fishmem_starmap.html          记忆星图
  fishmem_workers.sh            ingest / life / digest 三进程管理
deploy/
  install.sh                    一键安装（插件 + tools + systemd）
  systemd/                      三个服务单元
  requirements.txt              额外依赖（只有 qzone 需要）
  patches/patch_imgfix.py       可选：智谱多模态 400 的修复
docs/
  安装.md  架构.md  配置.md  防禁言.md  依赖清单.md
config/
  fish_memory.config.example.json
scripts/
  sync-from-server.sh           从服务器拉代码（本地开发用）
  check-secrets.sh              推送前敏感信息扫描
```

## 文档

| 文档 | 内容 |
|---|---|
| [安装](docs/安装.md) | 环境、systemd、首次验证 |
| [架构](docs/架构.md) | 五层模型、注入流水线、三个必须知道的历史坑 |
| [配置](docs/配置.md) | 全部关键配置项与热重载边界 |
| [防禁言](docs/防禁言.md) | 频率、节流、隐私外发、处置流程 |
| [依赖清单](docs/依赖清单.md) | 已验证的版本矩阵与 19 个插件版本 |
| [部署](deploy/README.md) | 一键安装、验证、回滚 |
| [路线图](docs/路线图.md) | v1.1.1 范围：真记忆、闭嘴判断、分层遗忘、四层互通 |
| [插件清单](docs/插件清单.md) | 本机装过哪些插件及版本 |

## 数据与隐私

运行时数据全部在 `data/plugin_data/` 下，**不进仓库**：

- `memory.db` —— 记忆内容和注入账本（**含聊天记录**）
- `emotion.db` / `life.db` / `style.db` —— 情绪、生活、表达
- 面板和星图都在 `127.0.0.1` 上，对外请走 SSH 隧道

仓库里不包含任何真实 QQ 号、服务器地址或 API Key。所有示例都用 `123456789` 这类占位值。

## 推送前必做

仓库里不应该出现真实 QQ 号、服务器地址或密钥。三层防线：

```bash
# 1) 本地黑名单：把真实值写进 .deny-list（该文件已被忽略，不会上传）
#    每行一个：真实 QQ、群号、服务器 IP、密码……
# 2) 提交前钩子
git config core.hooksPath .githooks
# 3) 推送前全量扫描（工作区 + 全部提交历史）
bash scripts/check-secrets.sh
```

`check-secrets.sh` 同时检查通用密钥特征（`sk-`、私钥块、赋值型密码）和 `.deny-list` 里的真实值，覆盖工作区与整个历史。**退出码非 0 就不要 push。**

> ⚠️ 部分沙箱/受限环境里 git 钩子无法执行（会被静默跳过）。这种环境下务必手动跑 `check-secrets.sh`。

## 许可证

- 本项目自身代码：**MIT**。你可以随意使用代码，但请给自家 AI 吃点好的，别送去直播间赛博乞讨。
- `plugins/astrbot_plugin_qzone`：**GPL-3.0**（上游 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) 的修改版，遵循原许可证）

详见 [LICENSES.md](LICENSES.md)。

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 插件宿主与消息管线
- [Zhalslar](https://github.com/Zhalslar) —— qzone 插件上游
