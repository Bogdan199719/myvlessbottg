document.addEventListener('DOMContentLoaded', function () {
	function initializePasswordToggles() {
		const togglePasswordButtons = document.querySelectorAll('.toggle-password')
		togglePasswordButtons.forEach(button => {
			if (!button.getAttribute('aria-label')) {
				button.setAttribute('aria-label', 'Показать или скрыть значение')
			}
			button.addEventListener('click', function () {
				const parent =
					this.closest('.form-group') || this.closest('.password-wrapper')
				if (!parent) return

				const passwordInput = parent.querySelector('input')
				if (!passwordInput) return

				if (passwordInput.type === 'password') {
					passwordInput.type = 'text'
					this.textContent = '🙈'
				} else {
					passwordInput.type = 'password'
					this.textContent = '👁️'
				}
			})
		})
	}

	function setupBotControlForms() {
		const controlForms = document.querySelectorAll(
			'form[action*="start-shop-bot"], form[action*="stop-shop-bot"], form[action*="start-support-bot"], form[action*="stop-support-bot"]'
		)

		controlForms.forEach(form => {
			form.addEventListener('submit', function (event) {
				if (event.defaultPrevented) return
				const button = form.querySelector('button[type="submit"]')
				if (button) {
					button.disabled = true
					if (form.action.includes('start')) {
						button.textContent = 'Запускаю...'
					} else if (form.action.includes('stop')) {
						button.textContent = 'Останавливаю...'
					}
				}
				setTimeout(function () {
					window.location.reload()
				}, 1000) // 1 second
			})
		})
	}

	function setupConfirmationForms() {
		const confirmationForms = document.querySelectorAll('form[data-confirm]')
		confirmationForms.forEach(form => {
			form.addEventListener('submit', function (event) {
				const message = form.getAttribute('data-confirm')
				const expectedText = form.getAttribute('data-confirm-text')
				let confirmed = false
				if (expectedText) {
					const enteredText = prompt(
						`${message}\n\nДля подтверждения введите ID: ${expectedText}`
					)
					confirmed = enteredText !== null && enteredText.trim() === expectedText
				} else {
					confirmed = confirm(message)
				}
				if (!confirmed) {
					event.preventDefault()
				}
			})
		})
	}

	function setupToggleSections() {
		const sections = document.querySelectorAll('[data-toggle-section]')
		if (!sections.length) return

		function applyToggle(section) {
			const checkboxId = section.getAttribute('data-toggle-section')
			if (!checkboxId) return
			const checkbox = document.getElementById(checkboxId)
			if (!checkbox) return
			section.style.display = checkbox.checked ? '' : 'none'
		}

		sections.forEach(section => {
			applyToggle(section)
			const checkboxId = section.getAttribute('data-toggle-section')
			const checkbox = checkboxId ? document.getElementById(checkboxId) : null
			if (!checkbox) return
			checkbox.addEventListener('change', () => applyToggle(section))
		})
	}

	function setupCopyButtons() {
		const copyButtons = document.querySelectorAll('button.copy-button[data-copy-target]')
		if (!copyButtons.length) return

		async function copyText(text) {
			if (navigator.clipboard && navigator.clipboard.writeText) {
				await navigator.clipboard.writeText(text)
				return
			}
			const textarea = document.createElement('textarea')
			textarea.value = text
			textarea.style.position = 'fixed'
			textarea.style.top = '-1000px'
			document.body.appendChild(textarea)
			textarea.focus()
			textarea.select()
			try {
				document.execCommand('copy')
			} finally {
				document.body.removeChild(textarea)
			}
		}

		copyButtons.forEach(button => {
			if (!button.getAttribute('aria-label')) {
				button.setAttribute('aria-label', 'Копировать значение поля')
			}
			button.addEventListener('click', async function () {
				const targetId = button.getAttribute('data-copy-target')
				if (!targetId) return
				const input = document.getElementById(targetId)
				if (!input) return
				const value = input.value || ''
				if (!value) return

				const oldText = button.textContent
				try {
					await copyText(value)
					button.textContent = '✓'
					setTimeout(() => {
						button.textContent = oldText
					}, 800)
				} catch (e) {
					button.textContent = '×'
					setTimeout(() => {
						button.textContent = oldText
					}, 800)
				}
			})
		})
	}

	function initializeDashboardCharts() {
		const chartDataEl = document.getElementById('chart-data')
		const trendChartCanvas = document.getElementById('analyticsTrendChart')
		const paymentChartCanvas = document.getElementById('paymentMethodsChart')
		if (!chartDataEl || (!trendChartCanvas && !paymentChartCanvas)) {
			return
		}

		if (typeof Chart === 'undefined') {
			;[trendChartCanvas, paymentChartCanvas].filter(Boolean).forEach(canvas => {
				canvas.hidden = true
				const fallback = document.createElement('p')
				fallback.className = 'empty-state chart-fallback'
				fallback.setAttribute('role', 'status')
				fallback.textContent =
					'График временно недоступен. Числовые показатели выше остаются актуальными.'
				canvas.insertAdjacentElement('afterend', fallback)
			})
			return
		}

		let chartData
		try {
			chartData = JSON.parse(chartDataEl.textContent || '{}')
		} catch (e) {
			return
		}

		if (!chartData || !chartData.series) {
			return
		}

		const labels = Array.isArray(chartData.labels) ? chartData.labels : []
		const dates = Array.isArray(chartData.dates) ? chartData.dates : []
		const charts = []

		function valuesFor(seriesName) {
			const series = chartData.series[seriesName] || {}
			return dates.map(date => Number(series[date] || 0))
		}

		function compactMoney(value) {
			const amount = Number(value || 0)
			return `${amount.toLocaleString('ru-RU', {
				maximumFractionDigits: 0,
			})} ₽`
		}

		function compactNumber(value) {
			return Number(value || 0).toLocaleString('ru-RU')
		}

		function formatFullDate(isoDate) {
			const parts = String(isoDate || '').split('-')
			if (parts.length !== 3) return isoDate || ''
			return `${parts[2]}.${parts[1]}.${parts[0]}`
		}

		function baseScaleOptions() {
			const isMobile = window.innerWidth <= 768
			const isVerySmall = window.innerWidth <= 470

			return {
				x: {
					grid: {
						color: 'rgba(255, 255, 255, 0.05)',
					},
					ticks: {
						color: 'rgba(176, 186, 201, 0.9)',
						font: {
							size: isMobile ? 10 : 12,
						},
						maxTicksLimit: isMobile ? 8 : 15,
						maxRotation: 0,
						minRotation: 0,
						display: !isVerySmall,
					},
				},
				y: {
					beginAtZero: true,
					grid: {
						color: 'rgba(255, 255, 255, 0.06)',
					},
					ticks: {
						color: 'rgba(176, 186, 201, 0.9)',
						font: {
							size: isMobile ? 10 : 12,
						},
						display: !isVerySmall,
					},
				},
			}
		}

		function updateResponsiveOptions() {
			const isMobile = window.innerWidth <= 768
			const isVerySmall = window.innerWidth <= 470

			charts.forEach(chart => {
				if (chart.options.scales) {
					Object.assign(chart.options.scales, baseScaleOptions())
				}
				if (chart.options.plugins && chart.options.plugins.legend) {
					chart.options.plugins.legend.display = !isVerySmall
					chart.options.plugins.legend.labels.font.size = isMobile ? 11 : 12
				}
				chart.update()
			})
		}

		if (trendChartCanvas && labels.length && dates.length) {
			const trendChart = new Chart(trendChartCanvas.getContext('2d'), {
				type: 'bar',
				data: {
					labels: labels,
					datasets: [
						{
							label: 'Выручка, ₽',
							data: valuesFor('revenue'),
							borderColor: '#4da3ff',
							backgroundColor: 'rgba(77, 163, 255, 0.46)',
							borderWidth: 1,
							borderRadius: 5,
							maxBarThickness: 28,
							yAxisID: 'revenue',
							order: 2,
						},
						{
							label: 'Оплаты, шт.',
							data: valuesFor('orders'),
							type: 'line',
							borderColor: '#4cc38a',
							backgroundColor: '#4cc38a',
							borderWidth: 2,
							fill: false,
							tension: 0.28,
							yAxisID: 'count',
							pointRadius: 3,
							pointHoverRadius: 5,
							order: 1,
						},
						{
							label: 'Новые пользователи, шт.',
							data: valuesFor('users'),
							type: 'line',
							borderColor: '#f5a54a',
							backgroundColor: '#f5a54a',
							borderWidth: 2,
							fill: false,
							tension: 0.28,
							yAxisID: 'count',
							pointRadius: 3,
							pointHoverRadius: 5,
							borderDash: [5, 4],
							order: 0,
						},
					],
				},
				options: {
					responsive: true,
					maintainAspectRatio: false,
					interaction: {
						mode: 'index',
						intersect: false,
					},
					scales: {
						x: baseScaleOptions().x,
						revenue: {
							type: 'linear',
							position: 'left',
							beginAtZero: true,
							grid: {
								color: 'rgba(255, 255, 255, 0.06)',
							},
							ticks: {
								color: 'rgba(176, 186, 201, 0.9)',
								callback: value => compactMoney(value),
								display: window.innerWidth > 470,
							},
							title: {
								display: window.innerWidth > 470,
								text: 'Выручка',
								color: 'rgba(176, 186, 201, 0.9)',
							},
						},
						count: {
							type: 'linear',
							position: 'right',
							beginAtZero: true,
							grid: {
								drawOnChartArea: false,
							},
							ticks: {
								color: 'rgba(176, 186, 201, 0.9)',
								precision: 0,
								callback: value => compactNumber(value),
								display: window.innerWidth > 470,
							},
							title: {
								display: window.innerWidth > 470,
								text: 'Оплаты и пользователи',
								color: 'rgba(176, 186, 201, 0.9)',
							},
						},
					},
					plugins: {
						legend: {
							labels: {
								color: 'rgba(232, 237, 245, 0.92)',
								boxWidth: 10,
								boxHeight: 10,
								usePointStyle: true,
								font: {
									size: window.innerWidth <= 768 ? 11 : 12,
								},
							},
							display: window.innerWidth > 470,
						},
						tooltip: {
							backgroundColor: 'rgba(12, 16, 22, 0.96)',
							borderColor: 'rgba(255, 255, 255, 0.12)',
							borderWidth: 1,
							callbacks: {
								title: items => {
									const index = items && items.length ? items[0].dataIndex : 0
									return formatFullDate(dates[index])
								},
								label: context => {
									const label = context.dataset.label || ''
									if (context.dataset.yAxisID === 'revenue') {
										return `${label}: ${compactMoney(context.parsed.y)}`
									}
									return `${label}: ${compactNumber(context.parsed.y)}`
								},
								afterBody: items => {
									const index = items && items.length ? items[0].dataIndex : 0
									const date = dates[index]
									const revenue = Number((chartData.series.revenue || {})[date] || 0)
									const orders = Number((chartData.series.orders || {})[date] || 0)
									const users = Number((chartData.series.users || {})[date] || 0)
									if (!revenue && !orders && !users) {
										return ['Данных за день нет']
									}
									return [
										`Итого: ${compactMoney(revenue)}`,
										`Оплат: ${compactNumber(orders)}`,
										`Новых пользователей: ${compactNumber(users)}`,
									]
								},
							},
						},
					},
				},
			})
			charts.push(trendChart)
		}

		if (paymentChartCanvas) {
			const methods = Array.isArray(chartData.payment_methods)
				? chartData.payment_methods
				: []
			const paymentLabels = methods.map(item => item.method || 'Неизвестно')
			const paymentValues = methods.map(item => Number(item.revenue || 0))

			if (paymentLabels.length) {
				const paymentChart = new Chart(paymentChartCanvas.getContext('2d'), {
					type: 'doughnut',
					data: {
						labels: paymentLabels,
						datasets: [
							{
								data: paymentValues,
								backgroundColor: [
									'#4da3ff',
									'#4cc38a',
									'#f5a54a',
									'#ff6b6b',
									'#a78bfa',
									'#22d3ee',
								],
								borderColor: 'rgba(12, 16, 22, 0.96)',
								borderWidth: 3,
							},
						],
					},
					options: {
						responsive: true,
						maintainAspectRatio: false,
						cutout: '68%',
						plugins: {
							legend: {
								position: 'bottom',
								labels: {
									color: 'rgba(232, 237, 245, 0.92)',
									boxWidth: 10,
									boxHeight: 10,
									usePointStyle: true,
									font: {
										size: window.innerWidth <= 768 ? 11 : 12,
									},
								},
								display: window.innerWidth > 470,
							},
							tooltip: {
								backgroundColor: 'rgba(12, 16, 22, 0.96)',
								borderColor: 'rgba(255, 255, 255, 0.12)',
								borderWidth: 1,
								callbacks: {
									label: context => {
										const value = context.parsed || 0
										return `${context.label}: ${compactMoney(value)}`
									},
								},
							},
						},
					},
				})
				charts.push(paymentChart)
			}
		}

		window.addEventListener('resize', () => {
			updateResponsiveOptions()
		})
	}

	function setupUserTableFilters() {
		const searchInput = document.getElementById('usersSearch')
		const countEl = document.getElementById('usersCount')
		const filterButtons = document.querySelectorAll('[data-user-filter]')
		const table = document.querySelector('.users-table')
		if (!table || (!searchInput && !filterButtons.length)) {
			return
		}

		const rows = Array.from(table.querySelectorAll('tbody tr'))
		let activeFilter = 'all'

		function normalize(value) {
			return (value || '').toString().toLowerCase()
		}

		function applyFilters() {
			const query = normalize(searchInput ? searchInput.value : '')
			let visible = 0

			rows.forEach(row => {
				const status = normalize(row.dataset.status)
				const haystack = normalize(row.dataset.search || row.textContent)
				const matchesFilter =
					activeFilter === 'all' ||
					status === activeFilter
				const matchesQuery = !query || haystack.includes(query)
				const show = matchesFilter && matchesQuery
				row.style.display = show ? '' : 'none'
				if (show) visible += 1
			})

			if (countEl) {
				countEl.textContent = `${visible}/${rows.length}`
			}
		}

		if (searchInput) {
			searchInput.addEventListener('input', applyFilters)
		}

		filterButtons.forEach(button => {
			button.addEventListener('click', () => {
				filterButtons.forEach(btn => btn.classList.remove('is-active'))
				button.classList.add('is-active')
				activeFilter = button.getAttribute('data-user-filter') || 'all'
				applyFilters()
			})
		})

		applyFilters()
	}

	function setupKeyTableFilters() {
		const searchInput = document.getElementById('keysSearch')
		const countEl = document.getElementById('keysCount')
		const filterButtons = document.querySelectorAll('[data-key-filter]')
		const keyRows = Array.from(document.querySelectorAll('tr[data-key-row]'))
		const groupRows = Array.from(document.querySelectorAll('tr[data-group-row]'))

		if (!keyRows.length || (!searchInput && !filterButtons.length)) {
			return
		}

		let activeFilter = 'all'

		function normalize(value) {
			return (value || '').toString().toLowerCase()
		}

		function matchesStatus(rowStatus) {
			if (activeFilter === 'all') return true
			if (activeFilter === 'global') return rowStatus.type === 'global'
			if (activeFilter === 'trial') return rowStatus.trial === '1'
			if (activeFilter === 'expired') return rowStatus.status === 'expired'
			if (activeFilter === 'expiring') return rowStatus.status === 'expiring'
			if (activeFilter === 'active') return rowStatus.status === 'active'
			return true
		}

		function applyFilters() {
			const query = normalize(searchInput ? searchInput.value : '')
			let visibleRows = 0
			const visibleGroups = new Set()

			keyRows.forEach(row => {
				const rowStatus = {
					type: normalize(row.dataset.keyType),
					status: normalize(row.dataset.keyStatus),
					trial: normalize(row.dataset.keyTrial),
				}
				const haystack = normalize(row.dataset.keySearch || row.textContent)
				const matchFilter = matchesStatus(rowStatus)
				const matchQuery = !query || haystack.includes(query)
				const show = matchFilter && matchQuery
				row.style.display = show ? '' : 'none'
				if (show) {
					visibleRows += 1
					visibleGroups.add(row.dataset.group)
				}
			})

			groupRows.forEach(groupRow => {
				const groupId = groupRow.dataset.groupRow
				groupRow.style.display = visibleGroups.has(groupId) ? '' : 'none'
			})

			if (countEl) {
				countEl.textContent = `${visibleRows}/${keyRows.length}`
			}
		}

		if (searchInput) {
			searchInput.addEventListener('input', applyFilters)
		}

		filterButtons.forEach(button => {
			button.addEventListener('click', () => {
				filterButtons.forEach(btn => btn.classList.remove('is-active'))
				button.classList.add('is-active')
				activeFilter = button.getAttribute('data-key-filter') || 'all'
				applyFilters()
			})
		})

		applyFilters()
	}

	function setupMobileNavigation() {
		const toggle = document.getElementById('mobileNavToggle')
		const nav = document.getElementById('mainNavigation')
		if (!toggle || !nav) return

		toggle.addEventListener('click', () => {
			const isOpen = nav.classList.toggle('is-open')
			toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false')
		})

		nav.querySelectorAll('a').forEach(link => {
			link.addEventListener('click', () => {
				if (window.innerWidth <= 772) {
					nav.classList.remove('is-open')
					toggle.setAttribute('aria-expanded', 'false')
				}
			})
		})
	}

	function setupProfitDistributionPreview() {
		const select = document.getElementById('profitDistributionPeriod')
		const preview = document.getElementById('profitDistributionPreview')
		if (!select || !preview) return

		const fields = {
			period: preview.querySelector('[data-profit-preview="period"]'),
			revenue: preview.querySelector('[data-profit-preview="revenue"]'),
			tax: preview.querySelector('[data-profit-preview="tax"]'),
			serverCost: preview.querySelector('[data-profit-preview="serverCost"]'),
			bogdan: preview.querySelector('[data-profit-preview="bogdan"]'),
			vlad: preview.querySelector('[data-profit-preview="vlad"]'),
		}

		function applyPreview() {
			const option = select.options[select.selectedIndex]
			if (!option) return
			if (fields.period) fields.period.textContent = option.dataset.period || '—'
			if (fields.revenue) fields.revenue.textContent = option.dataset.revenue || '0'
			if (fields.tax) fields.tax.textContent = option.dataset.tax || '0'
			if (fields.serverCost) {
				fields.serverCost.textContent = option.dataset.serverCost || '0'
			}
			if (fields.bogdan) fields.bogdan.textContent = option.dataset.bogdan || '0'
			if (fields.vlad) fields.vlad.textContent = option.dataset.vlad || '0'
		}

		select.addEventListener('change', applyPreview)
		applyPreview()
	}

	initializePasswordToggles()
	setupConfirmationForms()
	setupBotControlForms()
	setupToggleSections()
	setupCopyButtons()
	initializeDashboardCharts()
	setupUserTableFilters()
	setupKeyTableFilters()
	setupMobileNavigation()
	setupProfitDistributionPreview()
})
