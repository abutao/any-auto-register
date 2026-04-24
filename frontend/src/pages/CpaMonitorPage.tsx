import { useCallback, useEffect, useState } from 'react'
import {
  Card,
  Table,
  Tag,
  Space,
  Button,
  Statistic,
  Row,
  Col,
  Typography,
  message,
  Badge,
  Timeline,
  Empty,
} from 'antd'
import {
  ReloadOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { apiFetch } from '../lib/utils'

const { Text, Title } = Typography

function statusTag(status: string, disabled: boolean, unavailable: boolean) {
  if (disabled) return <Tag color="default">已禁用</Tag>
  if (status === 'error') return <Tag color="red">错误</Tag>
  if (unavailable) return <Tag color="orange">不可用</Tag>
  if (status === 'active') return <Tag color="green">活跃</Tag>
  if (status === 'refreshing') return <Tag color="blue">刷新中</Tag>
  return <Tag>{status || '未知'}</Tag>
}

function formatTime(t: string) {
  if (!t) return '-'
  try {
    const d = new Date(t)
    return d.toLocaleString()
  } catch { return t }
}

export default function CpaMonitorPage() {
  const [status, setStatus] = useState<any>(null)
  const [logs, setLogs] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [maintaining, setMaintaining] = useState(false)

  const loadStatus = useCallback(async () => {
    setLoading(true)
    try {
      const [s, l] = await Promise.all([
        apiFetch('/cpa/status'),
        apiFetch('/cpa/logs'),
      ])
      setStatus(s)
      setLogs(l.logs || [])
    } catch (e: any) {
      message.error(e.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadStatus() }, [loadStatus])

  // 自动刷新
  useEffect(() => {
    const interval = setInterval(loadStatus, 15000)
    return () => clearInterval(interval)
  }, [loadStatus])

  const handleMaintain = async () => {
    setMaintaining(true)
    try {
      const res = await apiFetch('/cpa/maintain', { method: 'POST' })
      if (res.ok) {
        message.success('维护完成')
      } else {
        message.error(res.error || '维护失败')
      }
      loadStatus()
    } catch (e: any) {
      message.error(e.message || '维护失败')
    } finally {
      setMaintaining(false)
    }
  }

  if (!status) return null

  if (!status.configured) {
    return (
      <Card>
        <Empty description="CPA API URL 未配置，请在全局配置中设置 cpa_api_url" />
      </Card>
    )
  }

  const pool = status.pool || {}
  const files = status.files || []
  const threshold = status.threshold || 0

  const poolPercent = threshold > 0 ? Math.round((pool.active / threshold) * 100) : 0
  const poolColor = poolPercent >= 80 ? '#52c41a' : poolPercent >= 50 ? '#faad14' : '#ff4d4f'

  const columns = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 250,
      ellipsis: true,
      render: (t: string) => t || '-',
    },
    {
      title: '状态',
      key: 'status',
      width: 100,
      render: (_: any, r: any) => statusTag(r.status, r.disabled, r.unavailable),
    },
    {
      title: '文件',
      dataIndex: 'name',
      key: 'name',
      width: 200,
      ellipsis: true,
    },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (t: string) => formatTime(t),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 160,
      render: (t: string) => formatTime(t),
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Title level={4} style={{ margin: 0 }}>CPA 号池监控</Title>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadStatus} loading={loading}>刷新</Button>
          <Button type="primary" icon={<SyncOutlined />} onClick={handleMaintain} loading={maintaining}>
            手动维护
          </Button>
        </Space>
      </div>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={4}>
          <Card size="small">
            <Statistic
              title="活跃账号"
              value={pool.active || 0}
              suffix={`/ ${threshold}`}
              valueStyle={{ color: poolColor }}
              prefix={<CheckCircleOutlined />}
            />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic
              title="总数"
              value={pool.total || 0}
            />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic
              title="错误"
              value={pool.error || 0}
              valueStyle={{ color: pool.error > 0 ? '#ff4d4f' : undefined }}
              prefix={pool.error > 0 ? <CloseCircleOutlined /> : undefined}
            />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic
              title="不可用"
              value={pool.unavailable || 0}
              valueStyle={{ color: pool.unavailable > 0 ? '#faad14' : undefined }}
              prefix={pool.unavailable > 0 ? <ExclamationCircleOutlined /> : undefined}
            />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic title="阈值" value={threshold} />
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic
              title="自动维护"
              value={status.enabled ? '开启' : '关闭'}
              valueStyle={{ color: status.enabled ? '#52c41a' : '#999' }}
              suffix={status.enabled ? `/ ${status.interval_minutes}分钟` : ''}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col span={16}>
          <Card title={`号池列表 (${files.length})`} size="small">
            <Table
              rowKey="name"
              columns={columns}
              dataSource={files}
              size="small"
              pagination={{ pageSize: 20, showSizeChanger: true }}
              scroll={{ x: 900 }}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card title="维护日志" size="small" style={{ marginBottom: 16 }}>
            {logs.length === 0 ? (
              <Empty description="暂无日志" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <div style={{ maxHeight: 500, overflow: 'auto' }}>
                <Timeline
                  items={logs.slice(0, 20).map((log, i) => ({
                    color: log.register?.triggered ? 'blue' : 'green',
                    children: (
                      <div key={i}>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {formatTime(log.time)}
                        </Text>
                        <div>
                          <Badge
                            status={log.remaining >= (log.threshold || 0) ? 'success' : 'warning'}
                            text={`号池 ${log.remaining || 0} / ${log.threshold || 0}`}
                          />
                        </div>
                        {log.register?.triggered && (
                          <div>
                            <Tag color="blue">补注册 {log.register.count || '?'} 个</Tag>
                          </div>
                        )}
                      </div>
                    ),
                  }))}
                />
              </div>
            )}
          </Card>

          {Object.keys(status.providers || {}).length > 0 && (
            <Card title="Provider 分布" size="small">
              {Object.entries(status.providers).map(([k, v]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0' }}>
                  <Text>{k}</Text>
                  <Tag>{String(v)}</Tag>
                </div>
              ))}
            </Card>
          )}
        </Col>
      </Row>
    </div>
  )
}
