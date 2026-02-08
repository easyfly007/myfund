# 全球分散化基金监控系统

每日自动抓取40只基金净值，基于250日均线偏离度生成买入/卖出信号，通过邮件发送监控报告，并维护持仓快照文件。

## 快速开始

### 1. 安装依赖

```bash
pip install pyyaml
```

> Python 3.10+ 即可，无其他第三方依赖（使用标准库 urllib 抓取数据）。

### 2. 配置邮箱

编辑 `config.yaml` 中的 email 部分：

```yaml
email:
  enabled: true
  smtp_server: "smtp.qq.com"      # QQ邮箱
  smtp_port: 465
  use_ssl: true
  sender: "your_email@qq.com"     # ← 你的邮箱
  password: "abcdefghijklmnop"    # ← 授权码（非登录密码）
  receiver: "your_email@qq.com"   # ← 接收邮箱
```

**如何获取QQ邮箱授权码：**
QQ邮箱 → 设置 → 账户 → POP3/SMTP服务 → 开启 → 生成授权码

**其他邮箱：**
- 163邮箱: `smtp.163.com`, 端口 465
- Gmail: `smtp.gmail.com`, 端口 587, `use_ssl: false`（用STARTTLS）

### 3. 运行

```bash
# 首次运行（试运行，不写入交易记录，不发邮件）
python fund_monitor.py --dry-run --no-email

# 正常运行（计算信号 + 记录交易 + 更新持仓 + 发邮件）
python fund_monitor.py

# 仅查看报告不发邮件（保存HTML到 data/ 目录）
python fund_monitor.py --no-email

# 即使无信号也发送完整报告
python fund_monitor.py --report-all
```

### 4. 设置定时运行

**WSL2 crontab（推荐）：**
```bash
crontab -e
# 添加：每个交易日20:30运行
30 20 * * 1-5 cd /path/to/fund_monitor && python3 fund_monitor.py >> data/cron.log 2>&1
```

**Windows 任务计划程序：**
1. 打开 "任务计划程序"
2. 创建基本任务 → 名称: "基金监控"
3. 触发器: 每天，时间设为 20:30（等净值更新后）
4. 操作: 启动程序
   - 程序: `wsl` (如果用WSL2)
   - 参数: `-e bash -c "cd /path/to/fund_monitor && python3 fund_monitor.py"`

## 策略说明

### 核心逻辑

```
每日对每只基金：
├── 债券类（low波动）→ 不择时，配额未满则直接建仓信号
└── 权益/商品类 → 均线偏离策略：
    ├── 当前净值 vs 250日均价 → 计算偏离度
    ├── 偏离度 ≤ 买入阈值 → 买入信号
    │   ├── 配额未满 & 冷却期已过 → ✅ 执行买入
    │   ├── 冷却期内但偏离再扩大10% → ✅ 突破冷却，执行买入
    │   ├── 配额已满 & 较上次买入又跌20% & 未用过额外机会 → 🔥 超跌加仓（额外1份）
    │   └── 其他 → ❌ 仅通知
    ├── 偏离度 ≥ 卖出阈值 → 卖出信号
    │   ├── 有额外份额 & 冷却期已过 → ✅ 优先卖出额外份额
    │   ├── 有常规持仓 & 冷却期已过 → ✅ 卖出1份常规份额
    │   └── 无持仓 或 冷却中 → ❌ 仅通知
    └── 其他 → ⚪ 持有
```

### 阈值参数

| 波动等级 | 适用品种 | 买入阈值 | 卖出阈值 |
|---------|---------|---------|---------|
| mid | 宽基指数、黄金、成熟市场 | -15% | +30% |
| high | 行业/科技、新兴市场、原油 | -20% | +40% |
| low | 债券/固收 | 不择时 | 不择时 |

### 冷却期

- 买入冷却：30天（自然日）
- 卖出冷却：30天（自然日）
- 冷却突破：若偏离在冷却期内又扩大10%，可突破买入冷却
- 所有操作（buy / sell / extra_buy / extra_sell）之间互相遵守30天冷却

### 超跌加仓（extra share）

当某只基金的常规配额已满，但当前净值相比上次买入又下跌了20%以上时，在冷却期过后可额外买入1份。规则：

- 每只基金最多1次额外加仓机会（用过即失效）
- 额外份额不占常规50份配额
- 卖出时优先卖额外份额，卖完后等30天冷却再按常规策略卖常规份额
- 额外份额的买卖同样遵守30天冷却期

## 文件结构

```
fund_monitor/
├── config.yaml              # 配置文件（基金列表、阈值、邮箱）
├── fund_monitor.py          # 主程序
├── README.md                # 本文件
└── data/                    # 运行时自动生成
    ├── trade_log.csv        # 交易记录（每次执行信号后追加）
    ├── portfolio.csv        # 持仓快照（每次运行后自动刷新）
    └── report_YYYYMMDD.html # 本地报告（--no-email时生成）
```

### trade_log.csv 格式

| date | fund_code | action | nav | shares_delta | note |
|------|-----------|--------|-----|-------------|------|
| 2025-06-15 | 050025 | buy | 2.3456 | 1 | 偏离-16.2% |
| 2025-09-20 | 050025 | sell | 3.1234 | -1 | 偏离+32.1% |
| 2025-12-01 | 050025 | extra_buy | 1.9000 | 1 | 超跌加仓 |
| 2026-01-10 | 050025 | extra_sell | 2.5000 | -1 | 卖出额外份额 |

action 取值：`buy` / `sell` / `extra_buy` / `extra_sell`

### portfolio.csv 格式

每次运行后自动生成，反映当前持仓快照：

| fund_code | fund_name | category | regular_shares | max_shares | extra_shares | total_shares | avg_cost | last_buy_date |
|-----------|-----------|----------|---------------|------------|-------------|-------------|----------|--------------|
| 050025 | 博时标普500ETF联接A | 美国-标普500 | 2 | 2 | 0 | 2 | 1.2345 | 2025-06-15 |

程序假设你会手动执行买卖操作，trade_log 中的记录即为"计划"，portfolio.csv 反映计划执行后的持仓全貌。

## 自定义

### 调整阈值

编辑 `config.yaml` 中的 `strategy.thresholds` 部分。

### 添加/删除基金

在 `config.yaml` 的 `funds` 列表中增减条目：

```yaml
- code: "110020"
  name: "易方达沪深300ETF联接A"
  category: "A股-沪深300"
  volatility: mid
  max_shares: 2
```

### 手动记录已有持仓

如果你已经持有某些基金，需要手动在 `data/trade_log.csv` 中补录：

```csv
date,fund_code,action,nav,shares_delta,note
2025-01-15,050025,buy,2.1000,1,手动补录已有持仓
2025-01-15,050025,buy,2.1000,1,手动补录已有持仓
```

## 注意事项

1. **数据源限制**：天天基金API非官方接口，高频请求可能被限制，脚本内已设置0.5秒间隔
2. **QDII净值延迟**：QDII基金净值通常T+1或T+2公布，信号可能有滞后
3. **不构成投资建议**：本系统仅为辅助决策工具，请独立判断
4. **首次运行**：建议先 `--dry-run --no-email` 确认数据抓取正常
