import type { ReactNode } from 'react'

/** 中性分页契约(T-38):DataTable 薄壳内部渲染 Mantine Pagination */
export interface SrPagination {
  current?: number
  pageSize?: number
  total?: number
  onChange?: (page: number, pageSize: number) => void
  showSizeChanger?: boolean
  showTotal?: (total: number, range: [number, number]) => ReactNode
  /** 兼容字段:单页时隐藏分页条(ExperimentResults 传 hideOnSinglePage: true) */
  hideOnSinglePage?: boolean
}

export interface UseTablePaginationOptions {
  /** false 关闭分页(无限滚动模式);undefined 走默认分页 */
  pagination?: false | SrPagination
  /** pageSize 未显式指定时的默认值(默认 10,与 antd 默认一致) */
  defaultPageSize?: number
}

export interface TablePaginationResult {
  /** 归一化后的分页配置:false=无限滚动模式;对象=客户端分页(DataTable 内部渲染) */
  pagination: false | SrPagination
  /** 是否处于无限滚动模式(pagination=false;数据由外层哨兵增量注入) */
  infinite: boolean
}

/**
 * 分页 / 无限滚动双模式归一化 hook(自洽,无外部状态依赖,调用方零改动复用):
 * - pagination=false → 无限滚动模式:关闭分页,数据由外层增量注入;
 * - pagination=对象   → 补 pageSize 缺省值;total 存在而 current 缺失时补 current=1;
 * - pagination=undefined → 默认分页(pageSize=defaultPageSize)。
 * 禁止 import antd:契约与 antd TablePaginationConfig 正交,DataTable 层负责类型收窄。
 */
export function useTablePagination({
  pagination,
  defaultPageSize = 10,
}: UseTablePaginationOptions = {}): TablePaginationResult {
  if (pagination === false) {
    return { pagination: false, infinite: true }
  }
  const p: SrPagination = pagination ?? {}
  const out: SrPagination = { ...p, pageSize: p.pageSize ?? defaultPageSize }
  if (out.total != null && out.current == null) {
    out.current = 1
  }
  return { pagination: out, infinite: false }
}
