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

复制 `.env.example` 为 `.env` 并填 `LLM_API_KEY` 才会走模型。库文件在卷 `/data/qiyan.db`。

回滚：`docker compose down`，换回上一版镜像或 git 标签再 `build`。

## 云上函数

```
cd ResearchGuide-main/tools/deploy
python download_wheels.py
python deploy_fc.py
python deploy_fc.py qiyan-test
```

生产函数名 `qiyan`，测试函数名 `qiyan-test`。管理员兜底密钥只在本机 `.env` 和函数环境变量；学生自己的 DeepSeek 存在生产库的 `user_secrets`，不进镜像、不进 git。

## 账号与数据

- 登录：用户名 + 密码注册后，换浏览器能找回。访客仍可「先看看」，但换设备找不回；访客要保存自己的 DeepSeek 或换设备，须先升级成用户名密码。若开了公众号，网页给一个 6 位数字，学生关注后把数字发过去就能认回同一个号。会话令牌放请求头 `Authorization: Bearer`；所有接口默认要登录，公开的列在 `server/auth.py` 的 `PUBLIC`，请求里带的 `uid` 必须是登录的这个人。
- 每人一把 DeepSeek：登录后在「我的 DeepSeek」上传，探测成功才写入 `user_secrets`。调用时按这个人的 key 发请求，不写 `os.environ`。管理员「连接模型」仍只改整台机器的兜底 key。生产请设 `QIYAN_SECRET` 用来封装用户密钥。
- 为什么是公众号消息：网站扫码登录要微信开放平台企业认证 + 备案域名，公众号网页授权要认证服务号，个人都拿不到。个人订阅号能收用户消息，消息里带 openid（只对这个公众号有效），够认出是谁。不存手机号。密码只存哈希。
- 开通（一次）：
  1. 用个人身份注册一个订阅号（mp.weixin.qq.com，免费）。
  2. 后台「设置与开发 → 基本配置 → 服务器配置」：URL 填 `<接口地址>/api/wechat`（现在是 `https://qiyan-caxplsowco.cn-hangzhou.fcapp.run/api/wechat`），Token 填一串长随机字符，EncodingAESKey 点「随机生成」，消息加解密方式选「**安全模式**」，提交、启用。提交时微信会来验一次签名，所以要先把同一串 Token 设成函数的 `WECHAT_TOKEN`、EncodingAESKey 设成 `WECHAT_AES_KEY`、公众号 AppID 设成 `WECHAT_APP_ID`，发布之后再提交。
     为什么必须安全模式：明文模式下微信只签 token、timestamp、nonce，不签消息体；谁拿到一条签过名的回调 URL（函数访问日志里就有），五分钟内就能伪造「某个 openid 发了某个数字」登进别人的号。安全模式的 msg_signature 把密文签进去，消息体才可信。服务端设了 `WECHAT_AES_KEY` 就只收加密消息。
  3. 设 `WECHAT_ACCOUNT_ID`（公众号原始 ID，gh_ 开头，在「设置与开发 → 公众号设置」里）和 `WECHAT_ACCOUNT_NAME`，登录页就会显示关注二维码。二维码也可以下载下来放到页面里，用 `WECHAT_QR_URL` 指过去。
  没配公众号时页面只给访客入口。`AUTH_DEV_CODES=1` 只在本机用（登录页多一个「模拟微信发送」），配了 `WECHAT_TOKEN` 就自动失效。
- 函数计算必须尽量只跑一个实例，且库在会保留的盘上。微信的消息回调和网页的轮询是两个请求：落到两个实例上，各自的库里只有一半，登录就永远等不到。线上 SQLite 若仍写 `/tmp`，实例回收会丢掉账号和记录。`/api/health` 的 `db.ephemeral` 为 true 就是这种情况。前端遇到 `user not found` / 401 会清掉过期会话，请重新进入，不再把研读工具包显示成加载失败。`QIYAN_DB` 应指到会保留的盘。
- 微信要求消息接口五秒内回复，超时会重发两次；处理是幂等的。冷启动太慢的话，学生会看到「该公众号暂时无法提供服务」，重发一次就行。
- 备份：每天跑一次 `python tools/backup_db.py --out <备份目录> --keep 7`。隐私说明承诺删号后备份最多留 7 天，`--keep` 改大要同步改 `web/js/app.js` 的 `PRIVACY_HTML` 和 `server/auth.py` 的 `PRIVACY_VERSION`。
- 上线顺序：前端和接口一起发。旧前端不带令牌，新接口会一律回 401；老用户浏览器里只有 uid 的，新前端会自动认领一次（`/api/auth/legacy`）。
- 工作线程数默认 128（`QIYAN_THREADS`）。流式对话有总时限和字数封顶，并对 DeepSeek 关掉思考。
