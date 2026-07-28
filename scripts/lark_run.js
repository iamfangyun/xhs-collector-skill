#!/usr/bin/env node
// lark_run.js - Node.js 包装脚本，解决 Windows 下 Python subprocess 传中文参数的编码问题
// 用法: node lark_run.js <json_file> <lark_args...>
// 从 json_file 读取内容作为 --json 的值，其余参数原样传递
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

// 直接用 node.exe 执行 lark-cli 的 JS 入口（避免 .cmd 需要 shell:true 导致参数被 shell 拆分）
const NODE_EXE = process.execPath;
const LARK_ENTRY = String.raw`C:\Users\Administrator\.workbuddy\binaries\node\cli-connector-packages\node_modules\@larksuite\cli\scripts\run.js`;

const args = process.argv.slice(2);
if (args.length < 1) {
    process.stderr.write('Usage: node lark_run.js <json_file> <lark_args...>\n');
    process.exit(1);
}

const jsonFile = args[0];
const larkArgs = args.slice(1);

// 读取 JSON 文件内容
let jsonContent = '';
try {
    jsonContent = fs.readFileSync(jsonFile, 'utf-8');
} catch (e) {
    process.stderr.write(`Failed to read ${jsonFile}: ${e.message}\n`);
    process.exit(1);
}

// 替换 --json @file 占位符为实际内容
// 注意：原版用 for 循环 + i+=2，但 for 的 i++ 会多跳一个元素，导致 --as 被跳过 → "user" 变成 positional arg
// 改用 while 循环 + 显式 i 跳跃，避免 off-by-one
const finalArgs = [];
let i = 0;
while (i < larkArgs.length) {
    if (larkArgs[i] === '--json' && i + 1 < larkArgs.length && larkArgs[i+1].startsWith('@')) {
        finalArgs.push('--json', jsonContent);
        i += 2;  // 跳过 --json 和 @file，下一次循环从 @file 之后开始
    } else {
        finalArgs.push(larkArgs[i]);
        i += 1;
    }
}

// 用 node.exe 直接执行 run.js，不需要 shell（避免空格分割参数）
const result = spawnSync(NODE_EXE, [LARK_ENTRY, ...finalArgs], {
    encoding: 'utf-8',
    timeout: 60000,
    maxBuffer: 10 * 1024 * 1024,
    shell: false,  // 不用 shell，参数原样传递
});

process.stdout.write(result.stdout || '');
if (result.stderr) {
    process.stderr.write(result.stderr);
}
process.exit(result.status || 0);
