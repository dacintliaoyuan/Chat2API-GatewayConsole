# 云端控制台图标

`source.jpg` 是用户提供的原图，2048 × 1733，保留原始字节。`cutout.png` 是去除外部白底的透明版本，保留人物内部白色和署名；图标从透明版本等比导出，画布与网页样式不再铺白底。

在网关目录运行 `python -B tools/build-cloud-icons.py`（需要现有 Pillow），生成：

- `assets/UM云端控制台.ico`：多尺寸透明 ICO；`assets/UM云端控制台-transparent.ico` 是桌面使用的同内容副本，用独立路径避开旧白底图标缓存。
- `web/public/icons/cloud/`：云端侧栏品牌、浏览器标签与应用图标。
- `web/src/cloud/brand.ts`：云端挂载时选择这些资源，本地工作台继续使用原图标。

图标资源导出不启动服务，也不自动部署页面。本次源图 SHA256：`5cc9cb923806fc8c65b4fe8607b4052c9ec08ccbf0332b2f7cd17473bf4b006e`。
