import { createRouter, createWebHistory } from 'vue-router'

import Dashboard from '@/views/Dashboard.vue'
const Borehole = () => import('@/views/borehole/index.vue')
const Core = () => import('@/views/core/index.vue')
const QualityVersions = () => import('@/views/quality_versions/index.vue')
const Stratigraphy = () => import('@/views/stratigraphy/index.vue')
const Geophysics = () => import('@/views/geophysics/index.vue')
const Geochem = () => import('@/views/geochem/index.vue')
const Assay = () => import('@/views/assay/index.vue')
const Mapping = () => import('@/views/mapping/index.vue')
const SurveyPoint = () => import('@/views/survey_point/index.vue')
const DrillingLog = () => import('@/views/drilling_log/index.vue')
const Reserve = () => import('@/views/reserve/index.vue')
const SampleRegistry = () => import('@/views/sample_registry/index.vue')
const Equipment = () => import('@/views/equipment/index.vue')
const Hydro = () => import('@/views/hydro/index.vue')
const Section = () => import('@/views/section/index.vue')
const GeologicalReport = () => import('@/views/geological_report/index.vue')
const Remote = () => import('@/views/remote/index.vue')
const Mineral = () => import('@/views/mineral/index.vue')
const Environmental = () => import('@/views/environmental/index.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: Dashboard },
    { path: '/borehole', name: 'borehole', component: Borehole },
    { path: '/core', name: 'core', component: Core },
    { path: '/quality_versions', name: 'quality_versions', component: QualityVersions },
    { path: '/stratigraphy', name: 'stratigraphy', component: Stratigraphy },
    { path: '/geophysics', name: 'geophysics', component: Geophysics },
    { path: '/geochem', name: 'geochem', component: Geochem },
    { path: '/assay', name: 'assay', component: Assay },
    { path: '/mapping', name: 'mapping', component: Mapping },
    { path: '/survey_point', name: 'survey_point', component: SurveyPoint },
    { path: '/drilling_log', name: 'drilling_log', component: DrillingLog },
    { path: '/reserve', name: 'reserve', component: Reserve },
    { path: '/sample_registry', name: 'sample_registry', component: SampleRegistry },
    { path: '/equipment', name: 'equipment', component: Equipment },
    { path: '/hydro', name: 'hydro', component: Hydro },
    { path: '/section', name: 'section', component: Section },
    { path: '/geological_report', name: 'geological_report', component: GeologicalReport },
    { path: '/remote', name: 'remote', component: Remote },
    { path: '/mineral', name: 'mineral', component: Mineral },
    { path: '/environmental', name: 'environmental', component: Environmental },
  ],
})

export default router
