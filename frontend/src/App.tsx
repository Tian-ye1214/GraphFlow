import { useEffect } from 'react'
import { Layout, Menu, Spin } from 'antd'
import { BrowserRouter, Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import ModelsPage from './pages/ModelsPage'
import DatasetsPage from './pages/DatasetsPage'
import WorkflowsPage from './pages/WorkflowsPage'
import CanvasPage from './pages/CanvasPage'
import RunsPage from './pages/RunsPage'
import RunDetailPage from './pages/RunDetailPage'
import AgentDrawer from './agent/AgentDrawer'
import { useAuth } from './stores/auth'

function Shell() {
  const { user, ready } = useAuth()
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
            { key: '/runs', label: <Link to="/runs">运行记录</Link> },
          ]}
        />
      </Layout.Sider>
      <Layout.Content style={{ padding: 16 }}>
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
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
