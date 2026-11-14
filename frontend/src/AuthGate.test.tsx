// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthGate } from './AuthGate'

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('email account gate', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('stops a stalled login check and lets the user retry without reloading', async () => {
    vi.useFakeTimers()
    let firstSignal: AbortSignal | undefined
    let finishFirst!: (value: Response) => void
    const fetch = vi.fn()
      .mockImplementationOnce((_input: RequestInfo | URL, init?: RequestInit) => {
        firstSignal = init?.signal ?? undefined
        return new Promise<Response>((resolve) => { finishFirst = resolve })
      })
      .mockResolvedValueOnce(json({ error: { code: 'AUTH_REQUIRED' } }, 401))
    vi.stubGlobal('fetch', fetch)

    render(<AuthGate>{() => <main>工作台</main>}</AuthGate>)
    expect(screen.getByText('正在检查登录状态…')).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })

    expect(screen.getByRole('alert')).toHaveTextContent('登录状态检查超时')
    expect(firstSignal?.aborted).toBe(true)
    expect(screen.queryByText('工作台')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新连接' }))
    await act(async () => undefined)
    expect(screen.getByRole('heading', { name: '邮箱登录 / 注册' })).toBeInTheDocument()
    await act(async () => { finishFirst(json({ auth_required: false, user: null })) })
    expect(screen.getByRole('heading', { name: '邮箱登录 / 注册' })).toBeInTheDocument()
    expect(screen.queryByText('工作台')).not.toBeInTheDocument()
  })

  it('recovers from a server error and enters an existing session on retry', async () => {
    const user = { id: 'u_1', email: 'reader@example.com', is_admin: false }
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(json({}, 503))
      .mockResolvedValueOnce(json({ auth_required: true, user })))

    render(<AuthGate>{({ user }) => <main>欢迎 {user?.email}</main>}</AuthGate>)
    expect(await screen.findByRole('alert')).toHaveTextContent('无法检查登录状态')
    fireEvent.click(screen.getByRole('button', { name: '重新连接' }))
    expect(await screen.findByText('欢迎 reader@example.com')).toBeInTheDocument()
  })

  it('cancels StrictMode checks and does not time out after a successful check', async () => {
    vi.useFakeTimers()
    const signals: AbortSignal[] = []
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      signals.push(init?.signal as AbortSignal)
      return json({ auth_required: false, user: null })
    }))

    const { unmount } = render(<StrictMode><AuthGate>{() => <main>工作台</main>}</AuthGate></StrictMode>)
    await act(async () => undefined)
    expect(signals).toHaveLength(2)
    expect(signals[0].aborted).toBe(true)
    expect(signals[1].aborted).toBe(false)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(screen.getByText('工作台')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    unmount()
    expect(signals[1].aborted).toBe(true)
  })

  it('lets a verified email create an account and enter the app', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/auth/me') {
        return json({ error: { code: 'AUTH_REQUIRED', message: '请先登录。' } }, 401)
      }
      if (path === '/api/auth/request-code') return json({ message: '验证码已发送。' })
      if (path === '/api/auth/verify-code') {
        return json({ user: { id: 'u_1', email: 'reader@example.com', is_admin: false } })
      }
      if (path === '/api/auth/account') return json({ deleted_tasks: 0, files_pending_cleanup: 0 })
      throw new Error(`unexpected fetch: ${path}`)
    })
    vi.stubGlobal('fetch', fetch)

    render(<AuthGate>{({ user, deleteAccount }) => <><main>欢迎 {user?.email}</main><button onClick={() => void deleteAccount()}>删除账号与数据</button></>}</AuthGate>)
    expect(await screen.findByRole('heading', { name: '邮箱登录 / 注册' })).toBeInTheDocument()
    expect(screen.getByText('首次验证邮箱会自动创建账号。验证码 10 分钟内有效。')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('邮箱地址'), {
      target: { value: 'reader@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: '获取验证码' }))
    expect(await screen.findByText('验证码已发送，请检查收件箱和垃圾邮件。')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('邮箱验证码'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: '验证并进入溯衡' }))
    await waitFor(() => expect(screen.getByText('欢迎 reader@example.com')).toBeInTheDocument())
    expect(fetch).toHaveBeenCalledWith('/api/auth/verify-code', expect.objectContaining({ method: 'POST' }))
    fireEvent.click(screen.getByRole('button', { name: '删除账号与数据' }))
    expect(await screen.findByText('账号和任务记录已删除。')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith('/api/auth/account', { method: 'DELETE' })
  })
})
