import { useEffect } from 'react'
import { Alert, Button, Layout, Menu, Spin } from 'antd'
import { BrowserRouter, Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import ModelsPage from './pages/ModelsPage'
import PromptsPage from './pages/PromptsPage'
import DatasetsPage from './pages/DatasetsPage'
import WorkflowsPage from './pages/WorkflowsPage'
import CanvasPage from './pages/CanvasPage'
import RunsPage from './pages/RunsPage'
import RunDetailPage from './pages/RunDetailPage'
import ModelLogsPage from './pages/ModelLogsPage'
import AdminPage from './pages/AdminPage'
import AgentDrawer from './agent/AgentDrawer'
import { useAuth } from './stores/auth'

function Shell() {
  const { user, ready, logout, actAs } = useAuth()
  const location = useLocation()
  if (!ready) return <Spin style={{ display: 'block', marginTop: '20vh' }} />
  if (!user) return <Navigate to="/login" replace />
  const selected = '/' + (location.pathname.split('/')[1] || 'workflows')
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Sider theme="light">
        <div style={{ padding: 16, fontWeight: 700 }}>GraphFlow</div>
        <Menu
          selectedKeys={[selected]}
          items={[
            { key: '/workflows', label: <Link to="/workflows">工作流</Link> },
            { key: '/datasets', label: <Link to="/datasets">数据集</Link> },
            { key: '/models', label: <Link to="/models">模型配置</Link> },
            { key: '/prompts', label: <Link to="/prompts">提示词库</Link> },
            { key: '/runs', label: <Link to="/runs">运行记录</Link> },
            { key: '/model-logs', label: <Link to="/model-logs">模型日志</Link> },
            ...(user.is_admin ? [{ key: '/admin', label: <Link to="/admin">租户管理</Link> }] : []),
          ]}
        />
        <div style={{ position: 'absolute', bottom: 16, left: 16, right: 16,
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ color: '#666' }}>{user.display_name || user.username}</span>
          <Button size="small" onClick={() => void logout()}>退出</Button>
        </div>
      </Layout.Sider>
      <Layout.Content style={{ padding: 16 }}>
        {user.acting_as && (
          <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                 message={`正在以 ${user.acting_as} 身份操作`}
                 action={<Button size="small" onClick={() => void actAs(null)}>返回管理员</Button>} />
        )}
        <Outlet />
      </Layout.Content>
      <AgentDrawer />
    </Layout>
  )
}

export default function App() {
  const init = useAuth((s) => s.init)
  useEffect(() => {
    void init()
  }, [init])
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<Shell />}>
          <Route path="/" element={<Navigate to="/workflows" replace />} />
          <Route path="/workflows" element={<WorkflowsPage />} />
          <Route path="/workflows/:id/canvas" element={<CanvasPage />} />
          <Route path="/datasets" element={<DatasetsPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/prompts" element={<PromptsPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
          <Route path="/model-logs" element={<ModelLogsPage />} />
          <Route path="/admin" element={<AdminPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
