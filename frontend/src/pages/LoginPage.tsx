import { useState } from 'react'
import { Button, Card, Input, message } from 'antd'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../stores/auth'

export default function LoginPage() {
  const [username, setUsername] = useState('')
  const login = useAuth((s) => s.login)
  const navigate = useNavigate()

  const submit = async () => {
    if (!username.trim()) return
    try {
      await login(username.trim())
      navigate('/')
    } catch (e) {
      message.error(String((e as Error).message))
    }
  }

  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: '20vh' }}>
      <Card title="GraphFlow 登录" style={{ width: 360 }}>
        <Input
          placeholder="输入用户名（开发模式）"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          onPressEnter={submit}
          autoFocus
        />
        <Button type="primary" block style={{ marginTop: 16 }} onClick={submit}>
          进入
        </Button>
      </Card>
    </div>
  )
}
