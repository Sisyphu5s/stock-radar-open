import { useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { KEY_PREFIXES } from './storageKeys'

const KEY_PREFIX = KEY_PREFIXES.sessionState

function persistState(key: string, value: unknown): void {
  try {
    sessionStorage.setItem(KEY_PREFIX + key, JSON.stringify(value))
  } catch {
  }
}

function loadState<T>(key: string, initial: T): T {
  try {
    const raw = sessionStorage.getItem(KEY_PREFIX + key)
    if (raw == null) return initial
    return JSON.parse(raw) as T
  } catch {
    return initial
  }
}

export function usePersistentState<T>(key: string, initial: T): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => loadState(key, initial))
  const setState: Dispatch<SetStateAction<T>> = (action) => {
    setValue((prev) => {
      const next = typeof action === 'function'
        ? (action as (prev: T) => T)(prev)
        : action
      persistState(key, next)
      return next
    })
  }
  return [value, setState]
}
