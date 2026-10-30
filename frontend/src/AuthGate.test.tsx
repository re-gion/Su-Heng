// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthGate } from './AuthGate'

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('email account gate', () => {
  afterEach(() => vi.unstubAllGlobals())

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
    expect(await screen.findByRole('heading', { name: '注册 / 登录' })).toBeInTheDocument()
    expect(screen.getByText('首次验证邮箱会自动创建账号。验证码 10 分钟内有效。')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('邮箱地址'), {
      target: { value: 'reader@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: '发送验证码' }))
    expect(await screen.findByText('验证码已发送，请检查收件箱和垃圾邮件。')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('邮箱验证码'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: '验证并进入' }))
    await waitFor(() => expect(screen.getByText('欢迎 reader@example.com')).toBeInTheDocument())
    expect(fetch).toHaveBeenCalledWith('/api/auth/verify-code', expect.objectContaining({ method: 'POST' }))
    fireEvent.click(screen.getByRole('button', { name: '删除账号与数据' }))
    expect(await screen.findByText('账号和任务记录已删除。')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith('/api/auth/account', { method: 'DELETE' })
  })
})
