import type { ReactNode } from "react"
import {
  BookOpen,
  Workflow,
  Library,
  Microscope,
  Code2,
  Database,
  MessageSquare,
  Dna,
  FileSearch,
  FlaskConical,
  History,
  Home,
  Pill,
  Settings2,
} from "lucide-react"

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { Separator } from "@/components/ui/separator"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "@/components/ui/sidebar"

export type Crumb = {
  label: string
  href?: string
}

const NAV_WORKSPACE = [
  { title: "总览", href: "/", icon: Home },
]

const NAV_DATABASES = [
  { title: "基因", href: "/knowledge/genes/", icon: Dna },
  { title: "疾病", href: "/knowledge/diseases/", icon: BookOpen },
  { title: "药物", href: "/knowledge/drugs/", icon: Pill },
]

const NAV_EVIDENCE = [
  { title: "临床证据", href: "/evidence/clinical/", icon: FileSearch },
  {
    title: "文本资料", href: "/evidence/sources/", icon: Library,
    children: [
      { title: "文献管理", href: "/evidence/sources/papers/" },
      { title: "指南管理", href: "/evidence/sources/guidelines/" },
    ],
  },
]

const NAV_TOOLS = [
  { title: "变异注释", href: "/tools/annotate/", icon: FlaskConical },
  { title: "任务历史", href: "/tools/jobs/", icon: History },
  { title: "氨基酸映射", href: "/knowledge/amino-acids/", icon: Pill },
]

const NAV_SYSTEM = [
  { title: "工作流", href: "/management/workflows/", icon: Workflow },
  { title: "数据更新", href: "/tools/data-updates/", icon: Database },
  { title: "引擎与设置", href: "/tools/engines/", icon: Settings2 },
  { title: "问题与经验", href: "/knowledge/issues/", icon: MessageSquare },
  { title: "教学验证", href: "/tools/teaching/", icon: Microscope },
  { title: "API 说明", href: "/docs/", icon: Code2 },
]

type NavItem = {
  title: string
  href?: string
  icon: typeof Home
  disabled?: boolean
  children?: { title: string; href: string }[]
}

function isActive(href: string) {
  let path = window.location.pathname
  const params = new URLSearchParams(window.location.search)
  if (path === "/knowledge/standards/") {
    path = params.get("namespace") === "RXNORM" ? "/knowledge/drugs/" : "/knowledge/diseases/"
  } else if (path === "/library/") {
    path = params.get("kind") === "guideline" ? "/evidence/sources/guidelines/"
      : params.get("kind") === "paper" ? "/evidence/sources/papers/" : "/evidence/sources/"
  } else if (path === "/knowledge/evidence/") path = "/evidence/clinical/"
  else if (path === "/workflows/") path = "/management/workflows/"
  return path === href
}

type AppShellProps = {
  title: string
  description?: string
  crumbs?: Crumb[]
  actions?: ReactNode
  children: ReactNode
}

function NavGroup({
  label,
  items,
}: {
  label: string
  items: NavItem[]
}) {
  return (
    <SidebarGroup>
      <SidebarGroupLabel>{label}</SidebarGroupLabel>
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map((item) => (
            <SidebarMenuItem key={item.href ?? item.title}>
              <SidebarMenuButton
                className="h-10"
                asChild={Boolean(item.href)}
                aria-label={item.disabled ? `${item.title}，规划中` : undefined}
                disabled={item.disabled}
                isActive={item.href ? isActive(item.href) : false}
                tooltip={
                  item.disabled ? `${item.title}（规划中）` : item.title
                }
              >
                {item.href ? (
                  <a href={item.href}>
                    <item.icon />
                    <span>{item.title}</span>
                  </a>
                ) : (
                  <>
                    <item.icon />
                    <span>{item.title}</span>
                  </>
                )}
              </SidebarMenuButton>
              {item.children && (
                <SidebarMenuSub>
                  {item.children.map((child) => (
                    <SidebarMenuSubItem key={child.href}>
                      <SidebarMenuSubButton asChild isActive={isActive(child.href)} className="h-9">
                        <a href={child.href} aria-current={isActive(child.href) ? "page" : undefined}>
                          <span>{child.title}</span>
                        </a>
                      </SidebarMenuSubButton>
                    </SidebarMenuSubItem>
                  ))}
                </SidebarMenuSub>
              )}
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  )
}

export function AppShell({
  title,
  description,
  crumbs = [],
  actions,
  children,
}: AppShellProps) {
  return (
    <SidebarProvider>
      <Sidebar collapsible="icon" variant="sidebar">
        <SidebarHeader className="px-3 py-5">
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton size="lg" asChild>
                <a href="/">
                  <div className="flex aspect-square size-9 items-center justify-center text-primary">
                    <Dna className="!size-8" />
                  </div>
                  <div className="grid flex-1 text-left text-sm leading-tight">
                    <span className="truncate text-xl font-semibold text-primary">biomed</span>
                    <span className="truncate text-xs text-muted-foreground">
                      生信与知识库
                    </span>
                  </div>
                </a>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarHeader>

        <SidebarContent>
          <NavGroup label="工作台" items={NAV_WORKSPACE} />
          <NavGroup label="知识库" items={NAV_DATABASES} />
          <NavGroup label="证据库" items={NAV_EVIDENCE} />
          <NavGroup label="生信工具" items={NAV_TOOLS} />
          <NavGroup label="管理" items={NAV_SYSTEM} />
        </SidebarContent>

        <SidebarFooter>
          <div className="px-2 text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">
            本地数据 · 版本可追溯
          </div>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      <SidebarInset>
        <header className="sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/80">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-2 h-4" />
          <Breadcrumb>
            <BreadcrumbList>
              <BreadcrumbItem className="hidden md:block">
                <BreadcrumbLink href="/">工作台</BreadcrumbLink>
              </BreadcrumbItem>
              {crumbs.map((crumb, index) => (
                <span key={`${crumb.label}-${index}`} className="contents">
                  <BreadcrumbSeparator className="hidden md:block" />
                  <BreadcrumbItem>
                    {crumb.href ? (
                      <BreadcrumbLink href={crumb.href}>{crumb.label}</BreadcrumbLink>
                    ) : (
                      <BreadcrumbPage>{crumb.label}</BreadcrumbPage>
                    )}
                  </BreadcrumbItem>
                </span>
              ))}
            </BreadcrumbList>
          </Breadcrumb>
          {actions ? <div className="ml-auto flex items-center gap-2">{actions}</div> : null}
        </header>

        <div className="flex min-w-0 flex-1 flex-col gap-6 p-4 md:p-6 lg:p-8">
          <div className="flex flex-col gap-2">
            <h1 className="font-heading text-2xl font-semibold tracking-tight lg:text-3xl">{title}</h1>
            {description ? (
              <p className="text-sm text-muted-foreground">{description}</p>
            ) : null}
          </div>
          {children}
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
