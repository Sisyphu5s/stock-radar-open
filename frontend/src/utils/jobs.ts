/** 任务类型 → 结果页路由的单一数据源（MainLayout 任务弹窗 / JobBar 任务中心 / Tasks 详情页共用）。
 *  params 为任务参数快照（可选）：模拟盘实验需 project_id 拼进 /paper 路由。 */
export function jobResultPath(jobType: string, jobId: number, params?: Record<string, unknown>): string {
  switch (jobType) {
    case 'gp_run':
      // GP 进化任务 → 专用进化详情页（按代展示 IC 进化曲线与表达式演化；/research/runs/:id → ?job=）
      return `/research/runs/${jobId}`
    case 'evaluate':
    case 'walk_forward':
      // T-128:Walk-forward 验证任务与评估同页展示（FactorEvaluation 处理 wf 载荷）
      return `/research/evaluation?job=${jobId}`
    case 'backtest':
    case 'backtest_opt':
      // T-128:组合优化回测与回测同页展示（Backtest 按 isOptJob 恢复）
      return `/research/backtests?job=${jobId}`
    case 'paper_experiment': {
      // 模拟盘实验 → 模拟盘项目页（选中对应项目）
      const pid = params?.project_id
      return pid != null ? `/paper?project=${encodeURIComponent(String(pid))}` : '/paper'
    }
    case 'market_scan':
      // 全市场扫描 → 信号中心（详情抽屉另有专用按钮，此处统一跳信号中心）
      return '/signals'
    case 'alpha101_score':
    case 'factor_tune':
    case 'dataset_build':
    case 'neural_train':
    case 'rl_train':
      // 这几类任务无专用结果页，回任务中心查看详情
      return '/tasks'
    default:
      return '/tasks'
  }
}
