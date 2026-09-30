<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">地质勘探数据管理平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向地质勘探的钻孔编录、岩心取样、物探数据、化探分析、测绘资料与储量估算的综合数据管理后台。</span>
        <span class="head-user">当前值班：{{ store.operator }} · {{ store.shiftLabel }}</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()

const navItems = [{ label: "运营概览", path: "/" }, { label: "钻孔编录", path: "/borehole" }, { label: "岩心管理", path: "/core" }, { label: "版本生效台", path: "/quality_versions" }, { label: "地层划分", path: "/stratigraphy" }, { label: "地球物理", path: "/geophysics" }, { label: "化探分析", path: "/geochem" }, { label: "化验数据", path: "/assay" }, { label: "地质填图", path: "/mapping" }, { label: "测绘控制", path: "/survey_point" }, { label: "钻探日志", path: "/drilling_log" }, { label: "储量估算", path: "/reserve" }, { label: "样品登记", path: "/sample_registry" }, { label: "勘探设备", path: "/equipment" }, { label: "水文地质", path: "/hydro" }, { label: "剖面编录", path: "/section" }, { label: "地质报告", path: "/geological_report" }, { label: "遥感解译", path: "/remote" }, { label: "矿产评价", path: "/mineral" }, { label: "环境地质", path: "/environmental" }]
</script>
