/* ==============================================================================
 * WE-MEET: BabyRay Premium Live Dashboard Controller (app.js)
 * ============================================================================== */

const API_URL = "/api/status";
let lastLogCount = 0;
const activeWorkersMap = new Map(); // workerId -> DOM Element mapping

// 실시간 로그 하이라이팅 파서
function colorizeLog(msg) {
    if (msg.includes("[DEAD") || msg.includes("경고") || msg.includes("실패") || msg.includes("Error") || msg.includes("에러") || msg.includes("장애")) {
        return `<span style="color: #f43f5e; font-weight: 600;">${msg}</span>`;
    }
    if (msg.includes("완료 성공") || msg.includes("성공") || msg.includes("SUCCESS") || msg.includes("가동") || msg.includes("연결 성공") || msg.includes("복구")) {
        return `<span style="color: #10b981; font-weight: 500;">${msg}</span>`;
    }
    if (msg.includes("[Scheduler Action]") || msg.includes("SCALE_OUT") || msg.includes("SCALE_IN") || msg.includes("트리거") || msg.includes("스케일아웃") || msg.includes("증설") || msg.includes("회수") || msg.includes("[Dynamic Scale-Out]") || msg.includes("[Dynamic Scale-In]")) {
        return `<span style="color: #c084fc; font-weight: 600;">${msg}</span>`;
    }
    if (msg.includes("Heartbeat 수신") || msg.includes("하트비트")) {
        return `<span style="color: #475569; font-size: 0.8rem;">${msg}</span>`;
    }
    return `<span style="color: #cbd5e1;">${msg}</span>`;
}

// 워커 카드 DOM 생성
function createWorkerCard(wid, info) {
    const card = document.createElement("div");
    card.className = "worker-card scale-up-enter";
    card.id = `worker-card-${wid}`;

    // 마지막 상태 캐싱 (장애 퇴출 감지용)
    card.dataset.lastHeartbeat = info.last_heartbeat;
    card.dataset.status = info.status;

    // FedAvg Map-Merge 상태 데코레이터 클래스 입히기
    if (info.status.includes("MAP")) {
        card.classList.add("worker-card-mapping");
    } else if (info.status.includes("MERGE")) {
        card.classList.add("worker-card-merging");
    }

    const now = Date.now() / 1000;
    const hbAge = Math.max(0, now - info.last_heartbeat).toFixed(1);
    const isWarning = hbAge > 5.0;

    const statusClass = info.status === "IDLE" ? "status-idle" : (info.status === "LAUNCHING" ? "status-launching" : "status-busy");
    const typeClass = `type-${info.node_type}`;
    const typeName = info.node_type === "on_demand" ? "On-Demand" : (info.node_type === "spot_a" ? "Spot-A" : "Spot-B");

    card.innerHTML = `
        <div class="worker-title-row">
            <span class="worker-id">${wid}</span>
            <span class="worker-badge-type ${typeClass}">${typeName}</span>
        </div>
        
        <div class="worker-status ${statusClass}" id="w-status-${wid}">
            ${info.status} ${isWarning ? '(지연)' : ''}
        </div>

        <div class="worker-stats">
            <div class="worker-stat-bar">
                <span>CPU 사용률</span>
                <div style="display:flex; align-items:center;">
                    <span id="w-cpu-val-${wid}">${(info.cpu || 0).toFixed(1)}%</span>
                    <div class="worker-mini-bar">
                        <div class="worker-mini-fill" id="w-cpu-bar-${wid}" style="width: ${info.cpu || 0}%; background-color: var(--accent-blue);"></div>
                    </div>
                </div>
            </div>
            <div class="worker-stat-bar">
                <span>Memory 사용률</span>
                <div style="display:flex; align-items:center;">
                    <span id="w-mem-val-${wid}">${(info.mem || 0).toFixed(1)}%</span>
                    <div class="worker-mini-bar">
                        <div class="worker-mini-fill" id="w-mem-bar-${wid}" style="width: ${info.mem || 0}%; background-color: var(--accent-purple);"></div>
                    </div>
                </div>
            </div>
        </div>

        <div class="worker-heartbeat" id="w-hb-${wid}">
            <span>Last Heartbeat</span>
            <span class="hb-seconds">${hbAge}초 전</span>
        </div>
    `;

    // 0.05초 뒤 트랜지션 엔터 효과
    requestAnimationFrame(() => {
        setTimeout(() => {
            card.classList.remove("scale-up-enter");
            card.classList.add("scale-up-enter-active");
        }, 50);
    });

    return card;
}

// 워커 카드 DOM 업데이트
function updateWorkerCard(wid, card, info) {
    const now = Date.now() / 1000;
    const hbAge = Math.max(0, now - info.last_heartbeat).toFixed(1);
    const isWarning = hbAge > 5.0;

    // 캐시 상태 갱신
    card.dataset.lastHeartbeat = info.last_heartbeat;
    card.dataset.status = info.status;

    // FedAvg Map-Merge 상태 데코레이터 클래스 갱신
    card.classList.remove("worker-card-mapping", "worker-card-merging");
    if (info.status.includes("MAP")) {
        card.classList.add("worker-card-mapping");
    } else if (info.status.includes("MERGE")) {
        card.classList.add("worker-card-merging");
    }

    // Status 배지
    const statusBadge = card.querySelector(`#w-status-${wid}`);
    if (statusBadge) {
        statusBadge.className = `worker-status ${info.status === "IDLE" ? "status-idle" : (info.status === "LAUNCHING" ? "status-launching" : "status-busy")}`;
        statusBadge.innerText = `${info.status} ${isWarning ? '(지연)' : ''}`;
    }

    // CPU / Mem 리드 수치 갱신
    const cpuVal = card.querySelector(`#w-cpu-val-${wid}`);
    const cpuBar = card.querySelector(`#w-cpu-bar-${wid}`);
    if (cpuVal) cpuVal.innerText = `${(info.cpu || 0).toFixed(1)}%`;
    if (cpuBar) cpuBar.style.width = `${info.cpu || 0}%`;

    const memVal = card.querySelector(`#w-mem-val-${wid}`);
    const memBar = card.querySelector(`#w-mem-bar-${wid}`);
    if (memVal) memVal.innerText = `${(info.mem || 0).toFixed(1)}%`;
    if (memBar) memBar.style.width = `${info.mem || 0}%`;

    // 하트비트 세컨드
    const hbBlock = card.querySelector(`#w-hb-${wid}`);
    if (hbBlock) {
        if (isWarning) {
            hbBlock.style.color = "var(--accent-red)";
            hbBlock.style.fontWeight = "700";
        } else {
            hbBlock.style.color = "var(--text-muted)";
            hbBlock.style.fontWeight = "400";
        }
        const secondsSpan = hbBlock.querySelector(".hb-seconds");
        if (secondsSpan) secondsSpan.innerText = `${hbAge}초 전`;
    }
}

// 워커 노드 실시간 갱신 오케스트레이션 (온디맨드/스팟 분산 Reconciler)
function reconcileWorkers(workersData) {
    const ondemandContainer = document.getElementById("ondemand-container");
    const spotContainer = document.getElementById("spot-container");
    if (!ondemandContainer || !spotContainer) return;

    const incomingWorkerIds = new Set(Object.keys(workersData));

    // 1. Placeholder 제거
    const odPlaceholder = ondemandContainer.querySelector(".empty-placeholder-mini");
    const spPlaceholder = spotContainer.querySelector(".empty-placeholder-mini");

    let odCount = 0;
    let spCount = 0;

    // 2. 분류 카운트 세기
    incomingWorkerIds.forEach(wid => {
        const info = workersData[wid];
        if (info.node_type === "on_demand") odCount++;
        else spCount++;
    });

    if (odPlaceholder && odCount > 0) ondemandContainer.removeChild(odPlaceholder);
    if (spPlaceholder && spCount > 0) spotContainer.removeChild(spPlaceholder);

    // 3. Scale-Out & Update
    incomingWorkerIds.forEach(wid => {
        const info = workersData[wid];
        const targetContainer = info.node_type === "on_demand" ? ondemandContainer : spotContainer;

        if (!activeWorkersMap.has(wid)) {
            // New worker detected -> Scale-Out animation!
            const newCard = createWorkerCard(wid, info);
            targetContainer.appendChild(newCard);
            activeWorkersMap.set(wid, newCard);
        } else {
            // Existing worker -> update metrics only
            const card = activeWorkersMap.get(wid);
            updateWorkerCard(wid, card, info);
        }
    });

    // 4. Scale-In / Preemption 감지 및 차별화 퇴출 애니메이션
    activeWorkersMap.forEach((card, wid) => {
        if (!incomingWorkerIds.has(wid)) {
            // Worker gone -> Scale-In/Failure exit animation!
            const lastHb = parseFloat(card.dataset.lastHeartbeat || "0");
            const lastStatus = card.dataset.status || "";
            const hbAge = (Date.now() / 1000) - lastHb;
            
            // 만약 하트비트가 3초 이상 지연되었거나, 유휴(IDLE)가 아닌 작업 상태 중에 사라졌다면 장애/선점 회수(Preemption/Failure)로 판단
            const isFailureExit = hbAge > 3.0 || lastStatus.includes("BUSY") || lastStatus.includes("MAP") || lastStatus.includes("MERGE");
            
            card.classList.remove("scale-up-enter-active");
            
            // 즉각적인 시각 피드백: 퇴장 상태 텍스트로 오버라이드
            const statusBadge = card.querySelector(`#w-status-${wid}`);
            if (statusBadge) {
                if (isFailureExit) {
                    statusBadge.className = "worker-status status-busy exit-aborted-text";
                    statusBadge.innerText = "ABORT (선점 회수)";
                    card.style.borderColor = "var(--accent-red)";
                } else {
                    statusBadge.className = "worker-status status-idle exit-scalein-text";
                    statusBadge.innerText = "SCALE-IN (순차 회수)";
                    card.style.borderColor = "var(--accent-purple)";
                }
            }
            
            if (isFailureExit) {
                card.classList.add("scale-down-exit-active", "exit-failed");
            } else {
                card.classList.add("scale-down-exit-active");
            }
            
            // 0.5초 트랜지션 완료 후 DOM에서 삭제
            setTimeout(() => {
                if (card.parentNode) {
                    card.parentNode.removeChild(card);
                }
                activeWorkersMap.delete(wid);

                // 만약 특정 풀이 다 비어버렸다면 placeholder 다시 복원
                const activeList = Array.from(activeWorkersMap.keys());
                const remainingOndemand = activeList.some(id => workersData[id] && workersData[id].node_type === "on_demand");
                const remainingSpot = activeList.some(id => workersData[id] && workersData[id].node_type !== "on_demand");

                if (!remainingOndemand && !ondemandContainer.querySelector(".empty-placeholder-mini")) {
                    ondemandContainer.innerHTML = `<div class="empty-placeholder-mini">워커 감지 중...</div>`;
                }
                if (!remainingSpot && !spotContainer.querySelector(".empty-placeholder-mini")) {
                    spotContainer.innerHTML = `<div class="empty-placeholder-mini">가용한 스팟 인스턴스가 없습니다.</div>`;
                }
            }, 500);
        }
    });
}

// Scale/Preemption Event Timeline Parser
function updateScaleTimeline(logs) {
    const container = document.getElementById("scale-timeline-container");
    if (!container) return;

    const timelineEvents = [];
    const maxEvents = 4; // 최대 4개 이벤트 유지

    logs.forEach(log => {
        // 로그 형식 분석: "[2026-07-04 16:15:45] [Docker SDK] ..."
        const timestampMatch = log.match(/^\[(.*?)\]\s(.*)$/);
        if (!timestampMatch) return;
        const timestamp = timestampMatch[1].split(" ")[1]; // HH:MM:SS 획득
        const msg = timestampMatch[2];

        if (msg.includes("신규 Spot 컨테이너 가동 완료") || msg.includes("워커 신규 등록")) {
            const workerIdMatch = msg.match(/ID='(.*?)'/);
            const wId = workerIdMatch ? workerIdMatch[1] : "worker";
            // 중복 기입 방지
            if (!timelineEvents.some(e => e.time === timestamp && e.desc.includes(wId))) {
                timelineEvents.push({
                    type: "scale-out",
                    badge: "SCALE-OUT",
                    time: timestamp,
                    desc: `${wId} 노드 가동 및 등록`
                });
            }
        } else if (msg.includes("IDLE 컨테이너 회수 성공") || msg.includes("워커 정상 퇴장")) {
            const workerIdMatch = msg.match(/ID='(.*?)'/) || msg.match(/회수 성공:\s*(.*?)$/);
            const wId = workerIdMatch ? workerIdMatch[1].replace(/['\s]/g, '') : "worker";
            if (!timelineEvents.some(e => e.time === timestamp && e.desc.includes(wId))) {
                timelineEvents.push({
                    type: "scale-in",
                    badge: "SCALE-IN",
                    time: timestamp,
                    desc: `${wId} 유휴 자원 순차 회수`
                });
            }
        } else if (msg.includes("스팟 강제 회수") || msg.includes("Eviction") || msg.includes("DEAD 노드 감지") || msg.includes("오프라인 처리")) {
            const workerIdMatch = msg.match(/대상:\s*(.*?)\s*\(/) || msg.match(/회수 완료:\s*(.*?)$/) || msg.match(/감지\]\s*(.*?)\s*노드가/);
            const wId = workerIdMatch ? workerIdMatch[1].trim() : "worker";
            if (!timelineEvents.some(e => e.time === timestamp && e.desc.includes(wId))) {
                timelineEvents.push({
                    type: "preempt",
                    badge: "PREEMPT",
                    time: timestamp,
                    desc: `${wId} 장애 강제 회수`
                });
            }
        }
    });

    if (timelineEvents.length === 0) {
        container.innerHTML = `<div class="empty-timeline-placeholder">스케일 변동 이력이 아직 없습니다.</div>`;
    } else {
        const latestEvents = timelineEvents.slice(-maxEvents).reverse();
        let html = "";
        latestEvents.forEach(evt => {
            const badgeClass = `badge-${evt.type}`;
            html += `
                <div class="timeline-item">
                    <span class="timeline-badge ${badgeClass}">${evt.badge}</span>
                    <span class="timeline-time">${evt.time}</span>
                    <span class="timeline-desc" title="${evt.desc}">${evt.desc}</span>
                </div>
            `;
        });
        container.innerHTML = html;
    }
}

// 실시간 대시보드 데이터 취합 및 업데이트 루프
async function updateDashboard() {
    try {
        const response = await fetch(API_URL);
        if (!response.ok) throw new Error("GCS HTTP status error");
        
        const data = await response.json();
        
        // GCS Status 상태 바인딩
        const statusContainer = document.getElementById("status-container");
        const statusText = document.getElementById("status-text");
        if (statusContainer) statusContainer.className = "system-status online";
        if (statusText) statusText.innerText = "GCS ONLINE";

        // 1. Global Stats Banner 갱신
        const schedulerModeVal = document.getElementById("scheduler-mode-val");
        const completedTasksVal = document.getElementById("completed-tasks-val");
        const failedTasksVal = document.getElementById("failed-tasks-val");
        const rlEpsilonVal = document.getElementById("rl-epsilon-val");

        if (data.scheduler_mode !== undefined && schedulerModeVal) {
            schedulerModeVal.innerText = data.scheduler_mode.toUpperCase();
        }
        if (data.total_completed !== undefined && completedTasksVal) {
            completedTasksVal.innerText = data.total_completed;
        }
        if (data.total_failed !== undefined && failedTasksVal) {
            failedTasksVal.innerText = data.total_failed;
        }
        if (data.q_epsilon !== undefined && rlEpsilonVal) {
            rlEpsilonVal.innerText = data.q_epsilon.toFixed(3);
        }

        // 2. Budget 예산 및 소비 비용 갱신
        const budgetVal = document.getElementById("budget-val");
        const spentVal = document.getElementById("spent-val");
        const budgetProgressBar = document.getElementById("budget-progress-bar");

        if (data.virtual_budget !== undefined) {
            const remaining = data.virtual_budget;
            const spent = Math.max(0, 10.0 - remaining); // 초기 예산 $10.0달러 기준
            
            if (budgetVal) budgetVal.innerHTML = `$${remaining.toFixed(4)}<span class="currency">USD</span>`;
            if (spentVal) spentVal.innerHTML = `$${spent.toFixed(4)}<span class="currency">USD</span>`;
            if (budgetProgressBar) {
                const progressPct = Math.min(100, (spent / 10.0) * 100);
                budgetProgressBar.style.width = `${progressPct}%`;
            }
        }

        // 2.5. 실시간 시간당 비용율 계산
        const hourlyRateVal = document.getElementById("hourly-rate-val");
        if (hourlyRateVal) {
            let totalHourlyRate = 0.0;
            const rates = data.nodes_config || {
                "on_demand": { "cost_per_hour": 0.710 },
                "spot_a": { "cost_per_hour": 0.220 },
                "spot_b": { "cost_per_hour": 0.120 }
            };
            Object.values(data.workers || {}).forEach(w => {
                const type = w.node_type.toLowerCase();
                const nodeInfo = rates[type] || {};
                totalHourlyRate += nodeInfo.cost_per_hour || 0.0;
            });
            hourlyRateVal.innerText = `$${totalHourlyRate.toFixed(3)}/hr`;
        }

        // 3. Host Resource
        const hostCpuVal = document.getElementById("host-cpu-val");
        const hostCpuBar = document.getElementById("host-cpu-bar");
        const hostMemVal = document.getElementById("host-mem-val");
        const hostMemBar = document.getElementById("host-mem-bar");
        const gpuVramVal = document.getElementById("gpu-vram-val");
        const gpuVramBar = document.getElementById("gpu-vram-bar");

        if (data.host_cpu !== undefined) {
            if (hostCpuVal) hostCpuVal.innerText = `${data.host_cpu.toFixed(1)}%`;
            if (hostCpuBar) hostCpuBar.style.width = `${data.host_cpu}%`;
        }
        if (data.host_mem !== undefined) {
            if (hostMemVal) hostMemVal.innerText = `${data.host_mem.toFixed(1)}%`;
            if (hostMemBar) hostMemBar.style.width = `${data.host_mem}%`;
        }
        if (data.gpu_free_vram !== undefined) {
            const freeVram = data.gpu_free_vram;
            if (freeVram === -1) {
                if (gpuVramVal) gpuVramVal.innerText = "N/A (No GPU)";
                if (gpuVramBar) gpuVramBar.style.width = "0%";
            } else {
                if (gpuVramVal) gpuVramVal.innerText = `${freeVram} MiB`;
                if (gpuVramBar) {
                    const pct = Math.min(100, (freeVram / 8192) * 100);
                    gpuVramBar.style.width = `${pct}%`;
                }
            }
        }

        // 4. Workers Reconciler 기동
        const workerCount = document.getElementById("worker-count");
        const workersList = Object.keys(data.workers || {});
        if (workerCount) workerCount.innerText = `총 ${workersList.length}대 가동 중`;
        reconcileWorkers(data.workers || {});

        // 5. Task Queue 갱신
        const queueContainer = document.getElementById("queue-container");
        const queueCount = document.getElementById("queue-count");
        const queueList = data.queue || [];
        if (queueCount) queueCount.innerText = `대기 ${queueList.length}개`;

        if (queueContainer) {
            if (queueList.length === 0) {
                queueContainer.innerHTML = `
                    <div class="empty-placeholder">
                        <div class="empty-icon">📥</div>
                        <p>대기열이 비어 있습니다.<br>클라이언트 부하 유입을 모니터링 중입니다.</p>
                    </div>
                `;
            } else {
                let html = "";
                const now = Date.now() / 1000;
                
                queueList.forEach(task => {
                    const secondsLeft = Math.max(0, task.deadline - now);
                    const isOverdue = secondsLeft <= 0;
                    const timeText = isOverdue ? "Deadline 초과!" : `${secondsLeft.toFixed(1)}초 남음`;
                    
                    // 가상 스케일 30초
                    const fillPct = isOverdue ? 0 : Math.min(100, (secondsLeft / 30) * 100);
                    
                    let barColor = "var(--accent-green)";
                    if (secondsLeft < 12) barColor = "orange";
                    if (secondsLeft < 5 || isOverdue) barColor = "var(--accent-red)";

                    // Map-Merge 특수 서브태스크 배지 시각화
                    let modelBadgeClass = "task-model-badge";
                    let modelText = task.model_type;
                    if (task.task_id.includes("-map-")) {
                        modelBadgeClass += " badge-purple";
                        modelText += " (MAP)";
                    } else if (task.task_id.includes("-merge")) {
                        modelBadgeClass += " badge-blue";
                        modelText += " (MERGE)";
                    }

                    html += `
                        <div class="task-card" style="${isOverdue ? 'border-color: rgba(244, 63, 94, 0.35);' : ''}">
                            <div class="task-header">
                                <span class="task-id">${task.task_id}</span>
                                <span class="${modelBadgeClass}">${modelText}</span>
                            </div>
                            <div class="task-details">
                                <span>Epochs: ${task.epochs}회</span>
                                <span style="font-weight: 700; color: ${barColor}">${timeText}</span>
                            </div>
                            <div class="task-deadline-bar">
                                <div class="task-deadline-fill" style="width: ${fillPct}%; background-color: ${barColor};"></div>
                            </div>
                        </div>
                    `;
                });
                queueContainer.innerHTML = html;
            }
        }

        // 6. Console Event Stream 갱신 및 Timeline 파서 작동
        const logs = data.logs || [];
        if (logs.length !== lastLogCount) {
            const consoleLogs = document.getElementById("console-logs");
            if (consoleLogs) {
                let logsHtml = "";
                logs.forEach(log => {
                    logsHtml += `<div class="console-line">${colorizeLog(log)}</div>`;
                });
                consoleLogs.innerHTML = logsHtml;
                consoleLogs.scrollTop = consoleLogs.scrollHeight;
            }
            lastLogCount = logs.length;

            // 실시간 타임라인 로그 추출 갱신
            updateScaleTimeline(logs);
        }
        
        // 5.5. AI Inference Conclusions (최종 결론 패널 바인딩)
        const conclusionContainer = document.getElementById("conclusion-container");
        const conclusionCountBadge = document.getElementById("conclusion-count");
        const conclusionsList = data.conclusions || [];
        
        if (conclusionCountBadge) conclusionCountBadge.innerText = `결론 ${conclusionsList.length}개 도출`;
        
        if (conclusionContainer) {
            if (conclusionsList.length === 0) {
                conclusionContainer.innerHTML = `
                    <div class="empty-placeholder" style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 24px; color: var(--text-muted); text-align: center;">
                        <div class="empty-icon" style="font-size: 2rem; margin-bottom: 8px;">💡</div>
                        <p style="margin: 0; font-size: 0.85rem;">도출된 분석 결론이 없습니다.<br>Map-Reduce 연산 완료 시, 병합 및 다수결 결과가 여기에 실시간으로 기록됩니다.</p>
                    </div>
                `;
            } else {
                let conclusionsHtml = "";
                conclusionsList.slice().reverse().forEach(c => {
                    const dateStr = new Date(c.timestamp * 1000).toLocaleTimeString();
                    let modelColor = "#3b82f6";
                    if (c.model_type.toUpperCase() === "CNN") modelColor = "#10b981";
                    if (c.model_type.toUpperCase() === "RNN") modelColor = "#8b5cf6";
                    if (c.model_type.toUpperCase() === "LSTM") modelColor = "#ec4899";
                    
                    conclusionsHtml += `
                        <div class="conclusion-card" style="background: rgba(255, 255, 255, 0.03); border-left: 4px solid ${modelColor}; padding: 12px; border-radius: 6px; font-family: 'Inter', sans-serif; box-shadow: 0 2px 8px rgba(0,0,0,0.15);">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 0.8rem; color: var(--text-muted);">
                                <span>태스크: <strong>${c.task_id}</strong> [${c.model_type}]</span>
                                <span>${dateStr}</span>
                            </div>
                            <div style="font-size: 0.95rem; color: #f1f5f9; font-weight: 500; line-height: 1.4;">
                                ${c.conclusion}
                            </div>
                        </div>
                    `;
                });
                conclusionContainer.innerHTML = conclusionsHtml;
            }
        }

    } catch (err) {
        const statusContainer = document.getElementById("status-container");
        const statusText = document.getElementById("status-text");
        if (statusContainer) statusContainer.className = "system-status offline";
        if (statusText) statusText.innerText = "GCS DISCONNECTED";
        console.error("Dashboard refresh disconnected:", err);
    }
}

// 1.2초 주기로 동적 모니터링 갱신 기동
setInterval(updateDashboard, 1200);
updateDashboard();
