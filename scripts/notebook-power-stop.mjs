#!/usr/bin/env node
// Notebook 关机：node notebook-power-stop.mjs [实例名，缺省取配置 instance，再缺省 main]
// 需先设置 NOTEBOOK_POWER_CONF 指向本地凭据 JSON，见 docs/notebook-自动开关机.md
import { run } from "./notebook-power-lib.mjs";
process.exit(await run("stop", process.argv[2]));
