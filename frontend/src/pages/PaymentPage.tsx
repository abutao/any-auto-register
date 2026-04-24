import { useCallback, useEffect, useState } from 'react'
import {
  Card,
  Form,
  Select,
  Button,
  InputNumber,
  Input,
  Checkbox,
  Table,
  Tag,
  Space,
  message,
  Typography,
  Alert,
  Modal,
  notification,
} from 'antd'
import { apiFetch } from '../lib/utils'

const { Text } = Typography

const COUNTRY_OPTIONS = [
  { value: 'US', label: '美国' },
  { value: 'KR', label: '韩国' },
  { value: 'SG', label: '新加坡' },
  { value: 'DE', label: '德国' },
  { value: 'GB', label: '英国' },
  { value: 'FR', label: '法国' },
  { value: 'JP', label: '日本' },
  { value: 'HK', label: '香港' },
  { value: 'AU', label: '澳大利亚' },
  { value: 'CA', label: '加拿大' },
]

const CHECKOUT_COUNTRY_OPTIONS = [
  { value: 'AUTO', label: '跟随地址国家' },
  { value: 'IT', label: '意大利' },
  { value: 'US', label: '美国' },
  { value: 'GB', label: '英国' },
  { value: 'DE', label: '德国' },
  { value: 'FR', label: '法国' },
  { value: 'FI', label: '芬兰' },
  { value: 'KR', label: '韩国' },
  { value: 'SG', label: '新加坡' },
  { value: 'AE', label: '阿联酋' },
  { value: 'CA', label: '加拿大' },
  { value: 'AU', label: '澳大利亚' },
  { value: 'JP', label: '日本' },
  { value: 'HK', label: '香港' },
]

const PLAN_OPTIONS = [
  { value: 'plus', label: 'Plus ($20/月)' },
  { value: 'business', label: 'Business ($25/月/座位)' },
]

function planTag(plan: string) {
  switch (plan) {
    case 'plus': return <Tag color="blue">Plus</Tag>
    case 'business': return <Tag color="purple">Business</Tag>
    case 'team': return <Tag color="cyan">Team</Tag>
    default: return <Tag>Free</Tag>
  }
}

function parseExtra(raw: string) {
  try { return JSON.parse(raw || '{}') } catch { return {} }
}

export default function PaymentPage() {
  const [accounts, setAccounts] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [paymentJobId, setPaymentJobId] = useState('')
  const [paymentStatus, setPaymentStatus] = useState<any>(null)
  const [autoBatchStatus, setAutoBatchStatus] = useState<any>(null)
  const [polling, setPolling] = useState(false)

  const [form] = Form.useForm()
  const [batchForm] = Form.useForm()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiFetch('/accounts?platform=chatgpt&page=1&page_size=500')
      const items = (data.items || []).map((a: any) => {
        const extra = parseExtra(a.extra_json)
        return {
          ...a,
          extra,
          plan_type: extra.plan_type || 'free',
          payment_status: extra.payment_status || '',
        }
      })
      setAccounts(items)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // 加载全局代理配置
  useEffect(() => {
    apiFetch('/config').then(cfg => {
      const proxy = cfg.default_proxy || 'http://127.0.0.1:7890'
      form.setFieldValue('proxy', proxy)
      batchForm.setFieldValue('proxy', proxy)
    }).catch(() => {})
  }, [form, batchForm])

  // 轮询支付任务状态
  useEffect(() => {
    if (!paymentJobId || !polling) return
    const interval = setInterval(async () => {
      try {
        const s = await apiFetch(`/payment/status/${paymentJobId}`)
        setPaymentStatus(s)
        if (s.status === 'done') {
          setPolling(false)
          load()
          // 弹出结果通知
          if (s.success > 0 && s.failed === 0) {
            notification.success({
              message: '支付全部成功',
              description: `成功升级 ${s.success} 个账号`,
              duration: 10,
            })
          } else if (s.success > 0) {
            notification.warning({
              message: '支付部分成功',
              description: `成功 ${s.success} 个，失败 ${s.failed} 个`,
              duration: 10,
            })
          } else {
            notification.error({
              message: '支付全部失败',
              description: `${s.failed} 个账号支付失败`,
              duration: 10,
            })
          }
          // 显示详细结果
          if (s.results && s.results.length > 0) {
            Modal.info({
              title: `支付结果（成功 ${s.success} / 失败 ${s.failed}）`,
              width: 600,
              content: (
                <div style={{ maxHeight: 400, overflow: 'auto' }}>
                  {s.results.map((r: any, i: number) => (
                    <div key={i} style={{ padding: '4px 0', borderBottom: '1px solid #f0f0f0' }}>
                      <Tag color={r.ok ? 'green' : 'red'}>{r.ok ? '成功' : '失败'}</Tag>
                      <Text>{r.email}</Text>
                      {r.error && <Text type="danger" style={{ marginLeft: 8 }}>{r.error}</Text>}
                      {r.plan && <Tag color="blue" style={{ marginLeft: 8 }}>{r.plan.toUpperCase()}</Tag>}
                    </div>
                  ))}
                </div>
              ),
            })
          }
        }
      } catch { setPolling(false) }
    }, 3000)
    return () => clearInterval(interval)
  }, [paymentJobId, polling, load])

  // 轮询自动批量状态
  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        const s = await apiFetch('/payment/auto-batch/status')
        setAutoBatchStatus(s)
      } catch {}
    }, 5000)
    return () => clearInterval(interval)
  }, [])

  const handlePayment = async () => {
    if (selectedIds.length === 0) {
      message.warning('请先选择账号')
      return
    }
    const values = form.getFieldsValue()
    try {
      const res = await apiFetch('/payment/start', {
        method: 'POST',
        body: JSON.stringify({
          account_ids: selectedIds,
          plan: values.plan,
          country: values.country,
          checkout_country: values.checkout_country,
          proxy: values.proxy || '',
          max_retries: values.max_retries || 5,
          headless: values.headless !== false,
          card_bin: values.card_bin || '',
        }),
      })
      if (res.ok) {
        setPaymentJobId(res.job_id)
        setPolling(true)
        message.success(`支付任务已启动: ${res.count} 个账号`)
      }
    } catch (e: any) {
      message.error(e.message || '启动失败')
    }
  }

  const handleStartAutoBatch = async () => {
    const values = batchForm.getFieldsValue()
    try {
      const res = await apiFetch('/payment/auto-batch/start', {
        method: 'POST',
        body: JSON.stringify(values),
      })
      message.info(res.message || '已启动')
    } catch (e: any) {
      message.error(e.message || '启动失败')
    }
  }

  const handleStopAutoBatch = async () => {
    await apiFetch('/payment/auto-batch/stop', { method: 'POST' })
    message.info('正在停止...')
  }

  const freeAccounts = accounts.filter(a => a.plan_type === 'free')

  const columns = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 250,
      ellipsis: true,
    },
    {
      title: '套餐',
      key: 'plan_type',
      width: 100,
      render: (_: any, r: any) => planTag(r.plan_type),
    },
    {
      title: '支付状态',
      key: 'payment_status',
      width: 100,
      render: (_: any, r: any) => {
        const ps = r.payment_status
        if (!ps) return <Text type="secondary">-</Text>
        if (ps === 'success') return <Tag color="green">成功</Tag>
        if (ps === 'processing') return <Tag color="blue">支付中</Tag>
        if (ps === 'failed') return <Tag color="red">失败</Tag>
        return <Tag>{ps}</Tag>
      },
    },
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 150,
      render: (t: string) => t ? new Date(t).toLocaleString() : '-',
    },
  ]

  return (
    <div>
      <Card title="手动支付" style={{ marginBottom: 16 }}>
        <Form form={form} layout="inline" initialValues={{
          plan: 'plus', country: 'US', checkout_country: 'AUTO',
          max_retries: 5, headless: true, proxy: 'http://127.0.0.1:7890',
          card_bin: '625003',
        }}>
          <Form.Item name="plan" label="套餐">
            <Select options={PLAN_OPTIONS} style={{ width: 180 }} />
          </Form.Item>
          <Form.Item name="country" label="地址国家">
            <Select options={COUNTRY_OPTIONS} style={{ width: 120 }} />
          </Form.Item>
          <Form.Item name="checkout_country" label="结账国家">
            <Select options={CHECKOUT_COUNTRY_OPTIONS} style={{ width: 150 }} />
          </Form.Item>
          <Form.Item name="card_bin" label="卡头">
            <Input placeholder="625003" style={{ width: 130 }} />
          </Form.Item>
          <Form.Item name="max_retries" label="重试次数">
            <InputNumber min={1} max={20} style={{ width: 70 }} />
          </Form.Item>
          <Form.Item name="proxy" label="代理">
            <Input placeholder="留空用默认代理" style={{ width: 200 }} />
          </Form.Item>
          <Form.Item name="headless" valuePropName="checked">
            <Checkbox>无头模式</Checkbox>
          </Form.Item>
        </Form>
        <div style={{ marginTop: 12 }}>
          <Space>
            <Button type="primary" onClick={handlePayment} disabled={selectedIds.length === 0 || polling} loading={polling}>
              {polling ? '支付中...' : `升级选中账号 (${selectedIds.length})`}
            </Button>
            {polling && paymentJobId && (
              <Button danger onClick={async () => {
                await apiFetch(`/payment/stop/${paymentJobId}`, { method: 'POST' })
                message.info('正在停止...')
              }}>
                停止支付
              </Button>
            )}
            {paymentStatus && (
              <Text type={paymentStatus.status === 'done' ? (paymentStatus.success > 0 ? 'success' : 'danger') : 'secondary'}>
                {paymentStatus.status === 'done'
                  ? `完成: 成功 ${paymentStatus.success}, 失败 ${paymentStatus.failed}`
                  : `进度: ${paymentStatus.progress} | 成功: ${paymentStatus.success} | 失败: ${paymentStatus.failed}`
                }
              </Text>
            )}
          </Space>
        </div>
      </Card>

      <Card title="定时批量支付" style={{ marginBottom: 16 }}>
        <Form form={batchForm} layout="inline" initialValues={{
          plan: 'plus', country: 'US', checkout_country: 'AUTO',
          batch_size: 10, interval_minutes: 10, max_batches: 0,
          max_retries: 5, headless: true,
        }}>
          <Form.Item name="plan" label="套餐">
            <Select options={PLAN_OPTIONS} style={{ width: 150 }} />
          </Form.Item>
          <Form.Item name="country" label="地址国家">
            <Select options={COUNTRY_OPTIONS} style={{ width: 100 }} />
          </Form.Item>
          <Form.Item name="checkout_country" label="结账国家">
            <Select options={CHECKOUT_COUNTRY_OPTIONS} style={{ width: 130 }} />
          </Form.Item>
          <Form.Item name="batch_size" label="每批">
            <InputNumber min={1} max={50} style={{ width: 60 }} />
          </Form.Item>
          <Form.Item name="interval_minutes" label="间隔(分)">
            <InputNumber min={1} max={120} style={{ width: 60 }} />
          </Form.Item>
          <Form.Item name="max_batches" label="最大批次">
            <InputNumber min={0} style={{ width: 70 }} placeholder="0=无限" />
          </Form.Item>
        </Form>
        <div style={{ marginTop: 12 }}>
          <Space>
            <Button
              type="primary"
              onClick={handleStartAutoBatch}
              disabled={autoBatchStatus?.running}
            >
              开始定时支付
            </Button>
            <Button
              danger
              onClick={handleStopAutoBatch}
              disabled={!autoBatchStatus?.running}
            >
              停止
            </Button>
            {autoBatchStatus && (
              <Text type="secondary">
                {autoBatchStatus.running ? '运行中' : '已停止'} |
                第 {autoBatchStatus.batch_num} 批 |
                成功: {autoBatchStatus.total_success} |
                失败: {autoBatchStatus.total_failed} |
                {autoBatchStatus.message}
              </Text>
            )}
          </Space>
        </div>
      </Card>

      {freeAccounts.length > 0 && (
        <Alert
          message={`当前有 ${freeAccounts.length} 个 Free 账号可升级`}
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      <Card title={`ChatGPT 账号 (${accounts.length})`}>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={accounts}
          loading={loading}
          size="small"
          pagination={{ pageSize: 50 }}
          rowSelection={{
            selectedRowKeys: selectedIds,
            onChange: (keys) => setSelectedIds(keys as number[]),
          }}
          scroll={{ x: 700 }}
        />
      </Card>
    </div>
  )
}
