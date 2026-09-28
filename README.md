# LexMask

面向法律文书的本地化脱敏工作台：判决书、笔录、合同、证据材料在本机或内网完成敏感信息识别、复核与真脱敏导出——原始文件不离开部署环境。

## 它做什么

上传文档 → OCR/版式解析 + 语义 NER（人物、机构、证件号、案号等）+ 视觉检测（印章、签字、证件、人脸、指印）→ 人工复核 → 打码 / 化名 / 替换输出。全程本地推理，支持批量队列与任务管理。

## 核心特性

- 中文与中英混排文书：PDF（含扫描件）、Word、图片、纯文本
- 文本链路：PP-StructureV3 OCR + HaS Text 语义 NER，正则兜底
- 视觉链路：LocateAnything-3B + OpenCV 骑缝章补全，章压文字找回
- 化名模式：组织分池化名、公共机构白名单保留
- PDF 双链路输出：MASK 真打码栅格化 / 替换走 docx 回转
- 国产算力：NVIDIA + 海光 DCU（DTK 25.04+）双栈
- 多用户隔离、Docker Compose 一键部署

## 快速开始

模型权重不入库，启动前放置：

| 服务 | 路径 | 来源 |
|---|---|---|
| ner | `backend/models/has/HaS_Text_0209_0.6B/` | [xuanwulab/HaS_Text_0209_0.6B](https://huggingface.co/xuanwulab/HaS_Text_0209_0.6B)（MIT） |
| visual-features | `backend/models/locateanything/LocateAnything-3B-HF/` | 官方权重（NVIDIA 非商用许可） |

```bash
docker compose --profile gpu up -d    # 打开 http://localhost:3000
```

本地开发与测试门禁：

```bash
cd backend && pytest tests/
cd frontend && npm run build && npm test && npm run lint
```

## 文档导航

| 主题 | 位置 |
|---|---|
| 云上部署（含 DCU 构建） | [cloud-deploy/README_CLOUD.md](./cloud-deploy/README_CLOUD.md) |
| 海光 DCU（K100_AI / DTK 26.04）部署教程 | [docs/deploy/](./docs/deploy/) |
| 海光 DCU 适配总览（镜像矩阵/卡型/坑清单） | [docs/dcu/海光DCU适配总览.md](./docs/dcu/海光DCU适配总览.md) |
| 开发工作流 / 测试门禁 / 仓库纪律 | [docs/WORKFLOW.md](./docs/WORKFLOW.md) · [docs/开发门禁-测试与独立评审规范.md](./docs/开发门禁-测试与独立评审规范.md) · [docs/协同开发规范-gh使用与仓库纪律.md](./docs/协同开发规范-gh使用与仓库纪律.md) |
| 用户使用说明 | [docs/用户使用说明.md](./docs/用户使用说明.md) |
| 性能与质量白皮书 | [docs/性能与质量白皮书.md](./docs/性能与质量白皮书.md) |
| NER 评测套件 | [eval/README.md](./eval/README.md) |

## 致谢

项目演进过程中参考了开源实现 [LexMask](https://github.com/ttttccxxui/LexMask)。

## 许可证

[Personal Use License 1.0](./LICENSE)——个人非商业使用免费；组织、生产与商业使用需商业授权，第三方组件授权亦须自行清理（详见 LICENSE 文件）。

