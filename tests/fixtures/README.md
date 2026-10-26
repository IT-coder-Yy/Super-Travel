# 界面测试照片

以下 JPEG 是 2026-10-26 从应用实际使用的 Wikimedia 缩略图响应缓存的原始字节，未再次编辑。只由 Playwright 业务测试返回，不进入生产应用；生产仍请求远程图片。

| 文件 | 作者与原作品 | 缩略尺寸 |
| --- | --- | --- |
| `west-lake-sunset.jpg` | [DXR：桥畔日落](https://commons.wikimedia.org/wiki/File:Sunset_over_the_Zigzag_Bridge,_West_Lake,_Hangzhou_20090909_1.jpg) | 960px |
| `west-lake-twilight.jpg` | [Tyler Valentine：西湖暮色](https://commons.wikimedia.org/wiki/File:West_lake_twilight.jpg) | 500px |

照片均按 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 使用，来源与产品的 README、设置页一致。

默认业务回归产生 `artifacts/workspace-fixture-*.png`，它们不证明远程图片可用。设置 `STA_LIVE_PHOTOS=1` 并仅运行标题含“真实 Wikimedia”的测试，会直接访问远程图片、记录响应/失败原因并生成 `artifacts/preview-network.png`。真实照片检查失败不会被静默替换为 fixture 成功。
