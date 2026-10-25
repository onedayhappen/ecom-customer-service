import { useState } from 'react'
import { Bot, ChevronRight, Headphones, LogOut, Package, Ticket, UserRound } from 'lucide-react'
import TaskConversation from './TaskConversation'
import CustomerProfile from './CustomerProfile'
import { useCustomerProfile } from './commerce/useCustomerProfile'
import CommerceList from './commerce/CommerceList'
import FilterTabs from './commerce/FilterTabs'
import { orderTabs, couponTabs } from './commerce/constants'

const sections = [
  { id: 'profile', label: '个人信息', icon: UserRound },
  { id: 'chat', label: '咨询客服', icon: Headphones },
  { id: 'orders', label: '订单列表', icon: Package },
  { id: 'coupons', label: '优惠券列表', icon: Ticket },
]
export default function CustomerWorkspace({ identity, onLogout, onStaff }) {
  const account = useCustomerProfile()
  const [section, setSection] = useState('chat')
  const [orderStatus, setOrderStatus] = useState('all')
  const [couponType, setCouponType] = useState('all')
  return (
    <div className={`app-shell customer-workspace${section === 'chat' ? ' chat-active' : ''}`}>
      <aside className="sidebar customer-sidebar">
        <div className="sidebar-brand">
          <span className="brand-mark small">
            <Bot size={22} />
          </span>
          优优客服
        </div>
        <nav className="customer-nav" aria-label="客户服务导航">
          {sections.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={`nav-item ${section === id ? 'active' : ''}`}
              aria-current={section === id ? 'page' : undefined}
              onClick={() => setSection(id)}
            >
              <span>
                <Icon size={18} />
                {label}
              </span>
              <ChevronRight size={14} />
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          {identity.role !== 'customer' && (
            <button className="side-action" onClick={onStaff}>
              <Headphones size={17} />
              客服工作台
            </button>
          )}
          <button className="side-action" onClick={onLogout}>
            <LogOut size={17} />
            退出
          </button>
          <div className="profile">
            <span className="profile-avatar">
              <UserRound size={17} />
            </span>
            <span>
              <strong>{account.profile?.username || identity.username || '我的账号'}</strong>
            </span>
          </div>
        </div>
      </aside>
      <div className="customer-content">
        {section === 'profile' && <CustomerProfile {...account} />}
        <div className="customer-chat" hidden={section !== 'chat'}>
          <TaskConversation identity={identity} />
        </div>
        {section === 'orders' && (
          <main className="commerce-page" aria-label="我的订单">
            <FilterTabs
              label="订单状态"
              tabs={orderTabs}
              value={orderStatus}
              onChange={setOrderStatus}
            />
            <CommerceList key={orderStatus} kind="orders" status={orderStatus} />
          </main>
        )}
        {section === 'coupons' && (
          <main className="commerce-page" aria-label="我的优惠">
            <FilterTabs
              label="优惠券类型"
              tabs={couponTabs}
              value={couponType}
              onChange={setCouponType}
            />
            <CommerceList key={`coupons-${couponType}`} kind="coupons" couponType={couponType} />
          </main>
        )}
      </div>
    </div>
  )
}
