document.addEventListener('DOMContentLoaded', function () {
	function initializePasswordToggles() {
		const togglePasswordButtons = document.querySelectorAll('.toggle-password')
		togglePasswordButtons.forEach(button => {
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
			form.addEventListener('submit', function () {
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
				if (!confirm(message)) {
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
		const usersChartCanvas = document.getElementById('newUsersChart')
		const chartDataEl = document.getElementById('chart-data')
		if (!usersChartCanvas || !chartDataEl) {
			return
		}

		let CHART_DATA
		try {
			CHART_DATA = JSON.parse(chartDataEl.textContent || '{}')
		} catch (e) {
			return
		}

		if (!CHART_DATA || !CHART_DATA.users || !CHART_DATA.keys) {
			return
		}

		function prepareChartData(data, label, color) {
			const labels = []
			const values = []
			const today = new Date()

			for (let i = 29; i >= 0; i--) {
				const date = new Date(today)
				date.setDate(today.getDate() - i)
				const dateString = date.toISOString().split('T')[0]
				const formattedDate = `${date.getDate().toString().padStart(2, '0')}.${(
					date.getMonth() + 1
				)
					.toString()
					.padStart(2, '0')}`
				labels.push(formattedDate)
				values.push(data[dateString] || 0)
			}

			return {
				labels: labels,
				datasets: [
					{
						label: label,
						data: values,
						borderColor: color,
						backgroundColor: color + '33',
						borderWidth: 2,
						fill: true,
						tension: 0.3,
					},
				],
			}
		}

		function updateChartFontsAndLabels(chart) {
			const isMobile = window.innerWidth <= 768
			const isVerySmall = window.innerWidth <= 470
			chart.options.scales.x.ticks.font.size = isMobile ? 10 : 12
			chart.options.scales.y.ticks.font.size = isMobile ? 10 : 12
			chart.options.plugins.legend.labels.font.size = isMobile ? 12 : 14
			chart.options.scales.x.ticks.maxTicksLimit = isMobile ? 8 : 15
			chart.options.scales.x.ticks.display = !isVerySmall
			chart.options.scales.y.ticks.display = !isVerySmall
			chart.options.plugins.legend.display = !isVerySmall
			chart.update()
		}

		const usersCtx = usersChartCanvas.getContext('2d')
		const usersChartData = prepareChartData(
			CHART_DATA.users,
			'Новых пользователей в день',
			'#007bff'
		)
		const usersChart = new Chart(usersCtx, {
			type: 'line',
			data: usersChartData,
			options: {
				scales: {
					y: {
						beginAtZero: true,
						ticks: {
							precision: 0,
							font: {
								size: window.innerWidth <= 768 ? 10 : 12,
							},
							display: window.innerWidth > 470,
						},
					},
					x: {
						ticks: {
							font: {
								size: window.innerWidth <= 768 ? 10 : 12,
							},
							maxTicksLimit: window.innerWidth <= 768 ? 8 : 15,
							maxRotation: 45,
							minRotation: 45,
							display: window.innerWidth > 470,
						},
					},
				},
				responsive: true,
				maintainAspectRatio: false,
				layout: {
					autoPadding: true,
					padding: 0,
				},
				plugins: {
					legend: {
						labels: {
							font: {
								size: window.innerWidth <= 768 ? 12 : 14,
							},
							display: window.innerWidth > 470,
						},
					},
				},
			},
		})

		const keysChartCanvas = document.getElementById('newKeysChart')
		if (!keysChartCanvas) return

		const keysCtx = keysChartCanvas.getContext('2d')
		const keysChartData = prepareChartData(
			CHART_DATA.keys,
			'Новых ключей в день',
			'#28a745'
		)
		const keysChart = new Chart(keysCtx, {
			type: 'line',
			data: keysChartData,
			options: {
				scales: {
					y: {
						beginAtZero: true,
						ticks: {
							precision: 0,
							font: {
								size: window.innerWidth <= 768 ? 10 : 12,
							},
							display: window.innerWidth > 470,
						},
					},
					x: {
						ticks: {
							font: {
								size: window.innerWidth <= 768 ? 10 : 12,
							},
							maxTicksLimit: window.innerWidth <= 768 ? 8 : 15,
							maxRotation: 45,
							minRotation: 45,
							display: window.innerWidth > 470,
						},
					},
				},
				responsive: true,
				maintainAspectRatio: false,
				layout: {
					autoPadding: true,
					padding: 0,
				},
				plugins: {
					legend: {
						labels: {
							font: {
								size: window.innerWidth <= 768 ? 12 : 14,
							},
							display: window.innerWidth > 470,
						},
					},
				},
			},
		})

		window.addEventListener('resize', () => {
			updateChartFontsAndLabels(usersChart)
			updateChartFontsAndLabels(keysChart)
		})
	}

	initializePasswordToggles()
	setupBotControlForms()
	setupConfirmationForms()
	setupToggleSections()
	setupCopyButtons()
	initializeDashboardCharts()
})
