import { FormEvent, ReactNode, useEffect, useState } from 'react'
import { AuthStatus, AuthUser, deleteMyAccount, getAuthStatus, logout, requestLoginCode, verifyLoginCode } from './api/client'
import brandLockup from './assets/brand/suheng-lockup.svg'
import './auth.css'

export type AuthView = {
  enabled: boolean
  user: AuthUser | null
  signOut: () => Promise<void>
  deleteAccount: () => Promise<void>
}

export function AuthGate({ children }: { children: (auth: AuthView) => ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [error, setError] = useState('')
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [cooldown, setCooldown] = useState(0)

  useEffect(() => {
    let active = true
    void getAuthStatus().then((value) => {
      if (active) setStatus(value)
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : '无法连接服务')
    })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (cooldown <= 0) return
    const timer = window.setTimeout(() => setCooldown((value) => Math.max(0, value - 1)), 1000)
    return () => window.clearTimeout(timer)
  }, [cooldown])

  const sendCode = async () => {
    setBusy(true)
    setNotice('')
    setError('')
    try {
      await requestLoginCode(email.trim())
      setCooldown(60)
      setNotice('验证码已发送，请检查收件箱和垃圾邮件。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '验证码发送失败，请稍后重试。')
    } finally {
      setBusy(false)
    }
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setBusy(true)
    setNotice('')
    setError('')
    try {
      const user = await verifyLoginCode(email.trim(), code.trim())
      setStatus({ auth_required: true, user })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '验证失败，请检查验证码。')
    } finally {
      setBusy(false)
    }
  }

  const signOut = async () => {
    try {
      await logout()
      setStatus({ auth_required: true, user: null })
      setCode('')
      setNotice('已退出登录。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '退出登录失败。')
    }
  }

  const deleteAccount = async () => {
    const result = await deleteMyAccount()
    setStatus({ auth_required: true, user: null })
    setCode('')
    setNotice(result.files_pending_cleanup > 0
      ? '账号和任务记录已删除；有部分本地文件未能自动清理，请联系管理员。'
      : '账号和任务记录已删除。')
    setError('')
  }

  if (!status) {
    return <main className="auth-shell"><section className="auth-card"><img src={brandLockup} alt="溯衡" width="138" height="64" /><p>{error || '正在检查登录状态…'}</p>{error && <button type="button" onClick={() => window.location.reload()}>重新连接</button>}</section></main>
  }

  if (status.auth_required && !status.user) {
    return <main className="auth-shell"><section className="auth-card">
      <img src={brandLockup} alt="溯衡" width="138" height="64" />
      <span className="section-kicker">账号验证</span>
      <h1>注册 / 登录</h1>
      <p>首次验证邮箱会自动创建账号。验证码 10 分钟内有效。</p>
      <form onSubmit={(event) => void submit(event)}>
        <label>邮箱地址<input type="email" autoComplete="email" required maxLength={254} value={email} onChange={(event) => setEmail(event.target.value)} placeholder="name@example.com" /></label>
        <div className="auth-code-row"><label>邮箱验证码<input inputMode="numeric" autoComplete="one-time-code" required pattern="[0-9]{6}" maxLength={6} value={code} onChange={(event) => setCode(event.target.value)} placeholder="6 位验证码" /></label><button className="auth-send" type="button" disabled={!email.trim() || busy || cooldown > 0} onClick={() => void sendCode()}>{cooldown > 0 ? `${cooldown} 秒后重发` : '发送验证码'}</button></div>
        <button className="auth-submit" type="submit" disabled={busy || code.length !== 6}>{busy ? '请稍候…' : '验证并进入'}</button>
      </form>
      {notice && <p className="auth-notice">{notice}</p>}
      {error && <p className="auth-error" role="alert">{error}</p>}
    </section></main>
  }

  return <>{children({ enabled: status.auth_required, user: status.user, signOut, deleteAccount })}</>
}
