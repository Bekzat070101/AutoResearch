# 启研云端发布（阿里云函数计算）

按量 ECS 在帐号后付费门槛不够时开不了。接口走杭州函数计算 HTTP 函数。

`*.fcapp.run` 会强制 `Content-Disposition: attachment`，浏览器会下载 htm。所以页面发布在 GitHub Pages：

https://chengcanwu.github.io/AutoResearch/

## 本机准备

1. 在 `product/.env` 写 `AccessKey_ID`、`AccessKey_Secret`，以及 `DEEPSEEK_API_KEY` 或 `LLM_API_KEY`。
2. 不要提交 `.env`、`.deploy.env`、`*.zip`、`vendor_wheels/`。

## 发布

在 `ResearchGuide-main/tools/deploy`（发布脚本要阿里云 SDK，用 uv 临时带上，不装进项目）：

```
python download_wheels.py
python deploy_fc.py qiyan-test
python deploy_fc.py          # 生产：库仍在 /tmp 时会拒绝，除非 --allow-ephemeral
python publish_gh_pages.py   # 前端，提交信息带界面 wNN
```

函数名 `qiyan`，地域 `cn-hangzhou`。公网地址写在已忽略的 `.deploy.env` 的 `PUBLIC_URL`。GetFunction 失败（除函数不存在）直接退出，不会把环境变量整组覆盖成 /tmp。

## 验收

- `GET $PUBLIC_URL/` 返回启研首页
- `GET $PUBLIC_URL/api/health` 返回 `ok: true`，且 `llm.enabled` 为 true

## 限制

SQLite 若仍在 `/tmp/qiyan.db`，实例回收后用户数据不会保留。NAS 挂盘是待确认项，没有持久盘不准把生产发版写成已完成。见 docs/DEPLOY.md。
