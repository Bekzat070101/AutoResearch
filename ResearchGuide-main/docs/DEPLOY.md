# 启研部署

控制台若问「Dockerfile 文件名」，填 `Dockerfile`。构建上下文是 `ResearchGuide-main`。

## 三套环境

对照课上的部署要求：开发用模拟数据，测试尽量与生产同构但不对外开放，生产才放真实用户和敏感信息。三套环境**各用各的库**，不要共用一份 `qiyan.db`。

| 环境 | 给谁 | 怎么起 | 库 |
| --- | --- | --- | --- |
| 开发 | 写代码的人 | `docker compose up qiyan` → http://127.0.0.1:8100 ；或直接跑 `server/main.py` | 本机卷 `/data/qiyan.db`，或 `QIYAN_DB` 指到本机文件。模拟数据。 |
| 测试 | 组内联调 | `docker compose up qiyan-test` → http://127.0.0.1:8101 ；云上是函数 `qiyan-test` | **另一份**库（测试卷或 `/tmp/qiyan-test.db`）。可用模拟或真实子集，不面向外网用户。 |
| 生产 | 真实用户 | 页面 https://chengcanwu.github.io/AutoResearch/ ；接口函数 `qiyan`（杭州） | `QIYAN_DB` 必须指到会留的盘（NAS / 云盘 / ECS 数据卷）。现在若仍是 `/tmp/qiyan.db`，`/api/health` 的 `db.ephemeral` 为 true，实例回收会丢掉账号。 |

生产库不和服务代码、不和前端静态文件混在一起。用户名单、密码哈希、每人的 DeepSeek 密钥、作业草稿和阅读记录都在这一份库里，按 `user_id` 隔离；**不要**给每个用户建一个 sqlite 文件，也不要把密钥写进镜像。

没有备案域名，生产首页不用 `fcapp.run`（会下载 htm）。没有开 ECS：帐号按量付费额度不够，不充值。挂上持久盘之后只改环境变量，不用改表结构。

## 本机容器

```
cd ResearchGuide-main
docker compose build
docker compose up qiyan
```

复制 `.env.example` 为 `.env` 并填 `LLM_API_KEY` 才会走模型（compose 用 `env_file` 把它传进容器；`.env` 本身不进镜像，需要 Compose 2.24 以上）。库文件在卷 `/data/qiyan.db`。

回滚：`docker compose down`，换回上一版镜像或 git 标签再 `build`。

## 云上函数

```
cd ResearchGuide-main/tools/deploy
uv run --no-project --with pip python download_wheels.py
uv run --no-project --with alibabacloud_fc20230330 --with alibabacloud_tea_openapi --with alibabacloud_tea_util python deploy_fc.py
uv run --no-project --with alibabacloud_fc20230330 --with alibabacloud_tea_openapi --with alibabacloud_tea_util python deploy_fc.py qiyan-test
```

生产函数名 `qiyan`，测试函数名 `qiyan-test`。管理员兜底密钥只在本机 `.env` 和函数环境变量，不入库；学生自己的 DeepSeek 封装后存在生产库的 `user_secrets`，不进镜像、不进 git（生产要设 `QIYAN_SECRET`）。发版时保留函数上已有的环境变量（控制台里配的 `WECHAT_*`、挂了 NAS 之后的 `QIYAN_DB`），只覆盖脚本自己管的几项；轮子里要有 `cryptography`（公众号安全模式），bootstrap 启动时会检查。

## 账号与数据

- 登录：用户名 + 密码。会话存在浏览器里（`localStorage` 的 `rg_uid` / `rg_token` / `rg_nick`），30 天不用失效；失效时提示重新登录，不静默落首页。页面入口只有登录 / 注册，没有「先看看」。`POST /api/auth/login` 仍是访客接口（测试还在），页面不要把它当入口。若开了公众号，网页给一个 6 位数字，学生关注后把数字发过去就能认回同一个号。会话令牌放请求头 `Authorization: Bearer`；所有接口默认要登录，公开的列在 `server/auth.py` 的 `PUBLIC`，请求里带的 `uid` 必须是登录的这个人。
- 每人一把 DeepSeek：登录后在「我的 DeepSeek」上传，探测成功才写入 `user_secrets`。调用时按这个人的 key 发请求，不写 `os.environ`。管理员「连接模型」仍只改整台机器的兜底 key。生产请设 `QIYAN_SECRET` 用来封装用户密钥。轮换顺序：**先设 `QIYAN_SECRET`，再 `POST /api/admin/rewrap`（body `old_secret`），再换 `LLM_API_KEY`**。只换 `LLM_API_KEY` 且没单独设 `QIYAN_SECRET` 时，封装密钥会跟着变，已存的个人 key 解不开（对话应回落规则版，前端提示重存，密文不删）。
- 为什么是公众号消息：网站扫码登录要微信开放平台企业认证 + 备案域名，公众号网页授权要认证服务号，个人都拿不到。个人订阅号能收用户消息，消息里带 openid（只对这个公众号有效），够认出是谁。不存手机号。密码只存哈希。
- 开通（一次）：
  1. 用个人身份注册一个订阅号（mp.weixin.qq.com，免费）。
  2. 后台「设置与开发 → 基本配置 → 服务器配置」：URL 填 `<接口地址>/api/wechat`（现在是 `https://qiyan-caxplsowco.cn-hangzhou.fcapp.run/api/wechat`），Token 填一串长随机字符，EncodingAESKey 点「随机生成」，消息加解密方式选「**安全模式**」，提交、启用。提交时微信会来验一次签名，所以要先把同一串 Token 设成函数的 `WECHAT_TOKEN`、EncodingAESKey 设成 `WECHAT_AES_KEY`、公众号 AppID 设成 `WECHAT_APP_ID`，发布之后再提交。
     为什么必须安全模式：明文模式下微信只签 token、timestamp、nonce，不签消息体；谁拿到一条签过名的回调 URL（函数访问日志里就有），五分钟内就能伪造「某个 openid 发了某个数字」登进别人的号。安全模式的 msg_signature 把密文签进去，消息体才可信。服务端设了 `WECHAT_AES_KEY` 就只收加密消息。
  3. 设 `WECHAT_ACCOUNT_ID`（公众号原始 ID，gh_ 开头，在「设置与开发 → 公众号设置」里）和 `WECHAT_ACCOUNT_NAME`，登录页就会显示关注二维码。二维码也可以下载下来放到页面里，用 `WECHAT_QR_URL` 指过去。
  没配公众号时页面只给用户名密码。`AUTH_DEV_CODES=1` 只在本机用（登录页多一个「模拟微信发送」），配了 `WECHAT_TOKEN` 就自动失效。
- 函数计算必须尽量只跑一个实例，且库在会保留的盘上。微信的消息回调和网页的轮询是两个请求：落到两个实例上，各自的库里只有一半，登录就永远等不到。线上 SQLite 若仍写 `/tmp`，实例回收会丢掉账号和记录。`/api/health` 的 `db.ephemeral` 为 true 就是这种情况。前端遇到 `user not found` / 401 会清掉过期会话，请重新进入，不再把研读工具包显示成加载失败。`QIYAN_DB` 应指到会保留的盘。
- 已注册账号跨发版：密码哈希在 SQLite `users` 表。库被换成空文件时，登录会显示「用户名或密码不对」，同名可以再注册——不是密码坏了，是人没了。设 `QIYAN_ACCOUNT_STORE` 指向**不和 QIYAN_DB 同块临时盘**的加密副本（例如 `/mnt/nas/qiyan-accounts.json.enc`），或设 `BACKUP_OSS_BUCKET` + `BACKUP_KEY`（对象在 `durable/`，不要放进 `daily/`）。注册/删号会更新副本；新空库启动时灌回同一批用户名和密码哈希，原密码仍能登录。对话、个人 DeepSeek、方向仍只在主库里，完整保留还是要挂 NAS。`/api/health` 的 `db.account_store.enabled` 为 true 表示副本开了。A5 仍待确认，没挂盘不准把生产发版写成已完成。
- 微信要求消息接口五秒内回复，超时会重发两次；处理是幂等的。冷启动太慢的话，学生会看到「该公众号暂时无法提供服务」，重发一次就行。
- 备份：以 OSS 生命周期为准（`POST /api/admin/backup` → `VACUUM INTO` → gzip → AES-GCM → `BACKUP_DIR` / OSS `daily/`）。隐私说明承诺删号后备份最多留 7 天；生命周期改成 >7 天必须同时改 `PRIVACY_HTML`、`PRIVACY_VERSION`、`PRIVACY_DIGEST` 并记 CHANGELOG。本机 `tools/backup_db.py` 只作开发备用。
- 上线顺序：前端和接口一起发。旧前端不带令牌，新接口会一律回 401；老用户浏览器里只有 uid 的，新前端会自动认领一次（`/api/auth/legacy`）。
- 只跑一个实例（再说一遍，因为下面几样都依赖它）：会话、微信登录的数字、每人一把的改动锁（userlock.py）、按来源限次、模型额度的内存部分，都在这一个进程和这一个库里。多实例时它们各管各的：登录来回失效、同一个人的改动又会互相覆盖。
- 模型额度（budget.py）：每天北京时间，全站 `LLM_DAILY_TOTAL`（默认 2000 次）、绑微信的人 `LLM_DAILY_USER`（150）、访客 `LLM_DAILY_GUEST`（15），全体访客合计 `LLM_DAILY_GUESTS`（500，刷访客号花不到绑微信的人头上）；上游重试也算一次；没同意隐私说明的人和匿名接口一律不调。超了功能回退到规则版，不报错。全站上限就是每天花费的上限。
- 线程：普通接口用 Starlette 工作线程 `QIYAN_THREADS`（128）；要等模型的活（对话、流式对话、交任务、方向推荐、项目检索）在 `QIYAN_MODEL_THREADS`（96）里跑，排队超过 `QIYAN_MODEL_QUEUE`（160）回 503「现在用的人太多」。`/api/health` 的 `model_pool` 报在跑和在排队的数目。流式对话开始了就跑完、自己放锁；还在排队时浏览器走了就撤掉、马上放锁。
- 限频：同一个公网出口一小时最多 `AUTH_PER_IP_HOUR`（600）次微信登录、600 个访客号（机房、宿舍几百人共用一个出口）。按出口地址限频要在代理后面设 `TRUST_PROXY=1`，否则认不出来源、不限。
- 上线前打开 `/api/health` 看两项：`auth.wechat_login` 是 true（安全模式配好了）、`db.ephemeral` 是 false（库在持久盘上）。
## 发版（没有持久盘不准把生产写成已完成）

```
cd ResearchGuide-main
python tools/verify/run_existing_tests.py
for f in web/js/*.js; do node --check "$f"; done
cd tools/deploy
python download_wheels.py
python deploy_fc.py qiyan-test
# 看输出的 QIYAN_DB。生产缺 QIYAN_SECRET / WECHAT_APP_ID、或 QIYAN_DB 在 /tmp 时脚本会拒绝，除非 --allow-ephemeral。
python deploy_fc.py          # 仅当 health.db.ephemeral=false
python publish_gh_pages.py   # 提交信息带 wNN 和源 sha
```

`deploy_fc.py`：GetFunction 除 FunctionNotFound 外直接退出；函数上已有 `QIYAN_DB` 时以它为准，改库用 `--db`；`reserved_concurrency=1` 失败则非零退出。

前端：`python tools/deploy/publish_gh_pages.py`。组装目录先清空，文件清单从 `index.html` 的 `static/` 引用解析。

## 待确认（控制台 / 钉钉 / RAM，未做完不要假装已挂盘）

**本轮（request-10-16）A5 状态：待确认。NAS 未挂上，不准假装已挂盘，不准把生产发版写成已完成。没挂盘前不要为了演示去发生产（发版会清 `/tmp` 库）。`refuse_unsafe` 已拦 `/private/tmp` 与未挂载的 `/data`；发版脚本会断言 `health.db.ephemeral=false`。两次发版后 `qiyan_users` 不变：仍待挂盘后验收。Pages 若已发到 w83，现网 API 仍是 w74，新页面打旧接口的 P0 未解除。**

本机 / 文档能做的步骤与验收命令：

```
# 1) 看现网是不是还在 /tmp（ephemeral=true 就是没挂盘）
curl -fsS $PUBLIC_URL/api/health
curl -fsS $PUBLIC_URL/api/health | python -c "import json,sys; d=json.load(sys.stdin); print(d['db']); assert 'ephemeral' in d['db']"

# 2) 挂盘后的验收（现在不应通过；通过才算 A5 完成）
#    控制台：qiyan 配 VPC + NAS 挂 /mnt/nas，环境变量 QIYAN_DB=/mnt/nas/qiyan.db
curl -fsS $PUBLIC_URL/api/health | python -c "import json,sys; d=json.load(sys.stdin); assert d['db']['ephemeral'] is False"
#    发两次版后：
curl -fsS -H "X-Admin-Token: $ADMIN_TOKEN" $PUBLIC_URL/api/admin/stats
#    用户数应与发版前相同。
```

演示口径：本机 `QIYAN_DB` 指向 `server/data/demo.db`（或其它非 `/tmp` 路径）即可演示；生产仍是 ephemeral 时只标待确认，不发生产。

1. 打开 `$PUBLIC_URL/api/health`，记下 `db.ephemeral`、`auth.wechat_login`、`llm.enabled`、`checks`。
2. 控制台核对：实例数限制是否为 1；是否已挂 NAS `/mnt/nas`；环境变量有无 `QIYAN_SECRET` / `WECHAT_APP_ID` / `TRUST_PROXY` / `ADMIN_TOKEN`；日志是否投递 SLS。`BACKUP_DIR` 挂盘后必须指到持久卷，不要再用 `/tmp/qiyan-backup`。
3. 先在 `qiyan-test` 验 NAS：挂同一盘、压测 SQLite 30 分钟并中途发版 2 次，看 `integrity_check` 和 `database is locked`，两次发版后用户数不变。不过关不切生产。
4. 持久盘（A5，待确认）：给 `qiyan` 配 VPC + NAS 挂 `/mnt/nas`，设 `QIYAN_DB=/mnt/nas/qiyan.db`。验收：`curl -fsS $PUBLIC_URL/api/health | python -c "import json,sys; d=json.load(sys.stdin); assert d['db']['ephemeral'] is False"`。发两次版后用户数不变。**没有这一步，禁止把生产发版写成已完成。**
5. 钉钉/企微群机器人：GitHub `secrets.ALERT_WEBHOOK` + `secrets.QIYAN_URL`。工作时间 1 小时内响应。未建群前探活失败只在 Actions 里红。
6. RAM 子账号：只给张效端 FC/OSS/BSS 只读发布权。未开子账号前不要用主账号 AK 当长期方案。
7. OSS 备份桶：私有、SSE-OSS、`daily/` 前缀 7 天删除。设 `BACKUP_OSS_BUCKET` 后备份才上云；没设时只写 `BACKUP_DIR`。
8. `/code` 是否只读：登录后 `curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $T" $TEST_URL/api/papers/2405.00001`。若 500，设 `ARXIV_CACHE_DIR=/tmp/papers`。论文正文现在进 SQLite `paper_text`（不再依赖随包 JSON）；`ARXIV_CACHE_DIR` 只作本机旧缓存导入。`/tmp` 期间 `ARXIV_CACHE_MAX_BYTES=100MB`。发版后若环境有 `ADMIN_TOKEN`，`deploy_fc.py` 会循环 `POST /api/admin/papers/warm` 直到 `remaining==0` 或 5 次；没有口令则打印「请手动预热」。没有 A5 时每次发版/回收都清库，预热必须幂等。**本轮 NAS 未挂上，不准把生产发版写成已完成。**

## 恢复（B5）

1. 设 `QIYAN_MAINTENANCE=1`（业务接口 503，`/api/admin/*` 和 `/api/health` 照常）。
2. `curl -XPOST -H "X-Admin-Token: $ADMIN_TOKEN" "$URL/api/admin/restore?object=qiyan-YYYYmmdd-HHMM.db.gz.enc"`
3. 按 SLS / 日志里的 `{"event":"user_deleted","uid":...}` 重放备份点之后的删号：`POST /api/admin/user/delete`。
4. 关掉维护模式，看 health 和用户数。
5. CHANGELOG 记事故。没有 NAS 和 OSS 备份时无法恢复，只能公告重新注册。

## 人工找回 / 删号（E8）

- 用户自己能在「我的」页导出或删号。
- 登不进去：用户名 + 密码是唯一自助入口；会话存在浏览器里，30 天不用失效。
- 忘了密码又没绑微信：系统没有找回密码。核实口径 = 注册用户名 + 最近一次对话内容（待确认由产品负责人拍板）。核实后管理员调 `POST /api/admin/user/delete`，工单只记日期和 uid。
- 绑了微信的用微信认回。

## 事故手册（通知组内群 + 产品负责人）

- F1 库丢了：`db.ephemeral=true`、探活失败、冷启动后 401 激增。止血：冻结发版和环境变量改动。有 NAS+OSS 按上面恢复；没有备份就无法恢复。A5 没完成不准再发版。
- F2 微信登录全失败：页面提示改用用户名密码；控制台补回 `WECHAT_*`。
- F3 模型全失败：功能退回规则版；充值或换钥匙。刚改隐私说明未重新同意的人额度为 0。
- F4 函数被打满：单实例 200 并发是硬上限。
- F5 Pages 坏了：按 `gh-pages` 上一版 sha 回滚。
- F6 误删单个用户：从最近备份 ATTACH 该 uid 各表行；用户自己删号有墓碑，不要恢复。
- F7 个人密钥全失效：多半是改了 `QIYAN_SECRET` / `LLM_API_KEY`，放回原值。
- F8 钥匙泄露：立即作废 RAM / DeepSeek / GitHub 令牌。

