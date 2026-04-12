# ImgShare Manager

中文说明在前，后面附英文版，方便直接发布到 GitHub。

## 中文简介

`ImgShare Manager` 是一个轻量的个人图床管理前端，配合 `MicroBin` 使用。

它主要解决这几个问题：

- 上传入口支持 Basic Auth 保护
- 图片分享页和图片直链可以公开访问
- 图片直链直接显示，不强制下载
- 上传页支持一次多张上传
- 默认保留时间为 3 个月，并扩展了 3/6/12 个月选项
- 首页带“已分享图片”管理界面
- 支持复制图片直链和删除图片

## 中文架构

- `imgshare-manager`：本项目，负责上传页和图片管理页
- `microbin`：负责实际存储和公开分享
- `caddy`：负责 HTTPS、认证和路由

## 中文目录结构

```text
.
├── app.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .dockerignore
├── Caddyfile.example
└── templates/
    └── index.html
```

## 中文功能

- 个人上传入口保护
- 公开图片分享页
- 公开图片直链
- 多图上传
- 图片列表管理
- 自定义保留时间
- 删除图片

## 中文快速开始

构建镜像：

```bash
docker build -t imgshare-manager:latest .
```

单独运行：

```bash
docker run -d \
  --name imgshare-manager \
  -p 8081:8081 \
  -v /opt/microbin/data:/data \
  -e MICROBIN_URL=http://microbin:8080 \
  -e DATABASE_PATH=/data/database.sqlite \
  -e ATTACHMENTS_PATH=/data/attachments \
  imgshare-manager:latest
```

使用 Compose：

```bash
docker compose up -d
```

## 中文环境变量

- `MICROBIN_URL`
  - 默认：`http://microbin:8080`
- `DATABASE_PATH`
  - 默认：`/data/database.sqlite`
- `ATTACHMENTS_PATH`
  - 默认：`/data/attachments`

## 中文访问方式

- 管理入口：`https://your-domain`
- 分享页：`https://your-domain/upload/<slug>`
- 图片直链：`https://your-domain/file/<slug>`

## 中文说明

这个项目本身不替代 `MicroBin`，而是给它补上一层更适合个人图床场景的管理界面。

---

## English Overview

`ImgShare Manager` is a lightweight personal image-hosting management frontend built to work with `MicroBin`.

It adds the missing management layer for a practical self-hosted image sharing setup:

- Basic Auth protection for the upload entry
- Public share pages and direct image links
- Direct image links rendered inline instead of forcing downloads
- Multi-file upload support
- Default retention set to 3 months, with 3/6/12 month options
- Built-in shared image management page
- Copy direct links and delete images from the UI

## Architecture

- `imgshare-manager`: upload page and image management UI
- `microbin`: storage and public sharing backend
- `caddy`: HTTPS, auth, and routing

## Project Structure

```text
.
├── app.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .dockerignore
├── Caddyfile.example
└── templates/
    └── index.html
```

## Features

- Protected upload entry
- Public share page
- Public direct image links
- Multi-file upload
- Image list management
- Custom retention periods
- Delete image support

## Quick Start

Build the image:

```bash
docker build -t imgshare-manager:latest .
```

Run standalone:

```bash
docker run -d \
  --name imgshare-manager \
  -p 8081:8081 \
  -v /opt/microbin/data:/data \
  -e MICROBIN_URL=http://microbin:8080 \
  -e DATABASE_PATH=/data/database.sqlite \
  -e ATTACHMENTS_PATH=/data/attachments \
  imgshare-manager:latest
```

Run with Compose:

```bash
docker compose up -d
```

## Environment Variables

- `MICROBIN_URL`
  - default: `http://microbin:8080`
- `DATABASE_PATH`
  - default: `/data/database.sqlite`
- `ATTACHMENTS_PATH`
  - default: `/data/attachments`

## URLs

- Admin entry: `https://your-domain`
- Share page: `https://your-domain/upload/<slug>`
- Direct image URL: `https://your-domain/file/<slug>`

## Notes

This project does not replace `MicroBin`. It adds a more practical management layer for personal image-hosting workflows.
