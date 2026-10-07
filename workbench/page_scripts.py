"""注入页面完成数据提取的脚本：Rewards 页面解析、账户信息、任务点击。

纯 JavaScript 字符串，由 worker 通过 Playwright evaluate 注入。"""
from __future__ import annotations


REWARDS_EXTRACTION_SCRIPT = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const uniq = (items) => {
    const seen = new Set();
    return items.filter((item) => {
      const key = JSON.stringify(item);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  };
  const bodyText = clean(document.body.innerText);
  const numberAfterLabel = (label, maxDistance = 220) => {
    const index = bodyText.indexOf(label);
    if (index < 0) return null;
    const snippet = bodyText.slice(index, index + maxDistance);
    const match = snippet.match(/(\d{1,3}(?:,\d{3})+|\d{1,7})/);
    return match ? parseInt(match[1].replace(/,/g, ''), 10) : null;
  };
  const getHeading = (headingText) => {
    const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p'));
    let best = null;
    for (const node of nodes) {
      const ownText = clean(Array.from(node.childNodes).filter((child) => child.nodeType === 3).map((child) => child.textContent).join(''));
      const fullText = clean(node.textContent);
      const isExact = ownText === headingText || (node.children.length === 0 && fullText === headingText);
      if (!isExact) continue;
      if (!best || node.querySelectorAll('*').length < best.querySelectorAll('*').length) best = node;
    }
    return best;
  };
  const getSection = (headingText) => {
    const heading = getHeading(headingText);
    if (!heading) return null;
    let node = heading;
    let best = heading.parentElement;
    for (let depth = 0; depth < 7 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const text = clean(node.innerText);
      const statusCount = (text.match(/已完成|未完成/g) || []).length;
      if (statusCount >= 1 && text.length < 12000) best = node;
      if (statusCount >= 2 || (statusCount >= 1 && text.length < 2500)) return node;
    }
    return best;
  };
  const getCard = (pointNode) => {
    let node = pointNode;
    let best = null;
    for (let depth = 0; depth < 8 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const text = clean(node.innerText);
      const statusCount = (text.match(/已完成|未完成/g) || []).length;
      if (statusCount === 1 && text.length >= 4 && text.length <= 420) best = node;
      if (statusCount > 1 || text.length > 420) break;
    }
    return best;
  };
  const extractTasks = (sectionName) => {
    const section = getSection(sectionName);
    if (!section) return [];
    const pointNodes = Array.from(section.querySelectorAll('div,p,span')).filter((node) => {
      const value = clean(node.innerText);
      const className = typeof node.className === 'string' ? node.className : '';
      const rewardBadge = /statusSuccess|statusInformative|statusWarning|reward/i.test(className);
      return rewardBadge && (/^\+\d{1,4}$/.test(value) || /^\d{1,4}$/.test(value) || /^\d{1,4}\s*积分$/.test(value));
    });
    const tasks = [];
    for (const pointNode of pointNodes) {
      const pointText = clean(pointNode.innerText);
      const pointMatch = pointText.match(/(\d{1,4})/);
      if (!pointMatch) continue;
      const points = parseInt(pointMatch[1], 10);
      if (!points) continue;
      const card = getCard(pointNode);
      if (!card) continue;
      const cardText = clean(card.innerText);
      if (!/已完成|未完成/.test(cardText)) continue;
      const completed = !cardText.includes('未完成') && cardText.includes('已完成');
      const status = completed ? '已完成' : '未完成';
      const lines = card.innerText.split(/\n+/).map(clean).filter(Boolean);
      const ignored = (line) => {
        const normalized = line.replace(/\s/g, '');
        return !line || normalized === '+' + points || normalized === String(points) || normalized === String(points) + '积分' || normalized === '已完成' || normalized === '未完成' || normalized === '积分' || /^[+*]?\d+$/.test(normalized);
      };
      const contentLines = lines.filter((line) => !ignored(line));
      const title = contentLines[0] || '';
      const description = contentLines.slice(1).join(' ').slice(0, 160);
      tasks.push({ title, description, points, status, completed });
    }
    const seen = new Set();
    return tasks.filter((task) => {
      const key = task.title + '|' + task.points + '|' + task.status;
      if (!task.title || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  };
  // 新版 dashboard（React 改版）的卡片不渲染“已完成/未完成”文字：
  // 完成状态优先取自页面内嵌的 Next.js 数据流（self.__next_f），失败时退回 DOM 特征。
  const flightFlags = (() => {
    const flags = {};
    try {
      const stream = (self.__next_f || [])
        .map((chunk) => (Array.isArray(chunk) && typeof chunk[1] === 'string' ? chunk[1] : ''))
        .join('');
      const pattern = /"isCompleted":(true|false)[\s\S]{0,400}?"offerId":"[^"]*","points":\d+,"title":"([^"]+)"/g;
      for (const match of stream.matchAll(pattern)) {
        const title = clean(match[2]);
        if (title) flags[title] = match[1] === 'true';
      }
    } catch (error) {}
    return flags;
  })();
  const extractNewUiTasks = (sectionName) => {
    // 新版 dashboard 专属：只认 #dailyset，避免混入旧版页面或进度面板的内容。
    const dailyset = document.querySelector('section#dailyset');
    if (!dailyset) return [];
    const cards = Array.from(dailyset.querySelectorAll('a[href]')).filter((card) => {
      const text = clean(card.innerText);
      if (!/\+\s*\d{1,4}/.test(text)) return false;
      if (/已完成|未完成/.test(text)) return false;
      return Boolean(card.querySelector('img[alt], p'));
    });
    if (!cards.length) return [];
    const seen = new Set();
    const tasks = [];
    for (const card of cards) {
      const image = card.querySelector('img[alt]');
      const alt = image ? clean(image.getAttribute('alt') || '') : '';
      const paragraphs = Array.from(card.querySelectorAll('p')).map((item) => clean(item.innerText)).filter(Boolean);
      const content = paragraphs.filter((line) => !/^\+\s*\d{1,4}$/.test(line.replace(/\s/g, '')));
      const title = alt || content[0] || '';
      if (!title || seen.has(title)) continue;
      seen.add(title);
      const description = content.filter((line) => line !== title).join(' ').slice(0, 160);
      const badge = clean(card.innerText).match(/\+\s*(\d{1,4})/);
      const points = badge ? parseInt(badge[1], 10) : null;
      let completed = flightFlags[title];
      if (typeof completed !== 'boolean') {
        const markup = card.innerHTML;
        completed = /statusSuccess/.test(markup) || markup.includes('m3.22 6.97-4.47 4.47');
      }
      tasks.push({ title, description, points, status: completed ? '已完成' : '未完成', completed });
    }
    return tasks;
  };
  const progressPairs = uniq(Array.from(bodyText.matchAll(/(\d{1,4})\s*\/\s*(\d{1,4})/g)).map((match) => ({
    text: match[0].replace(/\s+/g, ''),
    current: parseInt(match[1], 10),
    target: parseInt(match[2], 10),
  })));
  const progressByTarget = (target) => progressPairs.find((item) => item.target === target) || null;
  const edgeMatch = bodyText.match(/(?:Microsoft\s*)?Edge[^0-9]{0,80}?(\d{1,4})\s*\/\s*(\d{1,4})/i);
  const dailyMatch = bodyText.match(/日常任务\s*(\d{1,4})\s*\/\s*(\d{1,4})/);
  const searchRow = bodyText.match(/必应搜索\s+(\d{1,5})(?:\s|\/|$)/);
  const searchProgressMatch = bodyText.match(/必应搜索\s+(\d{1,5})\s*\/\s*(\d{1,5})/);
  const offerRow = bodyText.match(/优惠\s+(\d{1,5})(?:\s|$)/);
  const accountMatch = bodyText.slice(0, 1000).match(/\b\d{1,3}(?:,\d{3})+\b/);
  const loginRequired = /登录|Sign in|Sign-in/i.test(bodyText.slice(0, 1000)) && !/今日积分|积分明细|日常任务/.test(bodyText);
  const dailyTasks = extractTasks('日常任务');
  const dailyActivitiesNew = extractNewUiTasks('每日活动');
  const dailyActivities = dailyActivitiesNew.length ? dailyActivitiesNew : extractTasks('每日活动');
  return {
    url: location.href,
    title: document.title,
    captured_at: new Date().toISOString(),
    login_required: loginRequired,
    account_points: accountMatch ? parseInt(accountMatch[0].replace(/,/g, ''), 10) : null,
    today_points: numberAfterLabel('今日积分'),
    daily_task_progress: dailyMatch ? { current: parseInt(dailyMatch[1], 10), target: parseInt(dailyMatch[2], 10), text: dailyMatch[1] + '/' + dailyMatch[2] } : null,
    today_points_progress: progressByTarget(60),
    edge_browsing: edgeMatch ? { current: parseInt(edgeMatch[1], 10), target: parseInt(edgeMatch[2], 10), text: edgeMatch[1] + '/' + edgeMatch[2] } : null,
    search_row_points: searchRow ? parseInt(searchRow[1], 10) : null,
    search_progress: searchProgressMatch ? { current: parseInt(searchProgressMatch[1], 10), target: parseInt(searchProgressMatch[2], 10), text: searchProgressMatch[1] + '/' + searchProgressMatch[2] } : null,
    offer_row_points: offerRow ? parseInt(offerRow[1], 10) : null,
    progress_pairs: progressPairs,
    daily_tasks: dailyTasks,
    daily_activities: dailyActivities,
    daily_tasks_completed: dailyTasks.filter((task) => task.completed).length,
    daily_tasks_pending: dailyTasks.filter((task) => !task.completed).length,
    raw_text_preview: bodyText.slice(0, 1500),
  };
}
"""


REWARDS_ACCOUNT_EXTRACTION_SCRIPT = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const bodyText = clean(document.body.innerText);
  const lines = String(document.body.innerText || '').split(/\n+/).map(clean).filter(Boolean);
  const numberAfter = (label, maxDistance = 220) => {
    const index = bodyText.indexOf(label);
    if (index < 0) return null;
    const snippet = bodyText.slice(index, index + maxDistance);
    const match = snippet.match(/(\d{1,3}(?:,\d{3})+|\d{1,7})/);
    return match ? parseInt(match[1].replace(/,/g, ''), 10) : null;
  };
  const badgeNodes = Array.from(document.querySelectorAll('p,span,div')).filter((node) => {
    const text = clean(node.innerText);
    return /^(会员|银牌会员|金牌会员|铜牌会员)$/.test(text);
  });
  const badgeText = badgeNodes.length ? clean(badgeNodes[0].innerText) : (bodyText.match(/(金牌会员|银牌会员|铜牌会员|会员)/) || [])[1] || null;
  let username = null;
  if (badgeNodes.length) {
    let node = badgeNodes[0];
    for (let depth = 0; depth < 7 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const candidateLines = clean(node.innerText).split(/\n+/).map(clean).filter(Boolean);
      const index = candidateLines.findIndex((line) => line === badgeText);
      if (index > 0) {
        const candidate = candidateLines[index - 1];
        if (candidate && candidate.length <= 80 && !/可用积分|可领取|了解详细信息|Microsoft Rewards/i.test(candidate)) {
          username = candidate;
          break;
        }
      }
    }
  }
  if (!username && badgeText) {
    const index = lines.findIndex((line) => line === badgeText);
    if (index > 0) username = lines[index - 1];
  }
  const upgradeMatch = bodyText.match(/还需完成活动\s*[:：]?\s*(\d{1,4})/);
  const streakMatch = bodyText.match(/每日连续打卡\s*(\d{1,4})\s*天/);
  return {
    url: location.href,
    title: document.title,
    captured_at: new Date().toISOString(),
    username,
    membership_level: badgeText,
    available_points: numberAfter('可用积分'),
    claimable_points: numberAfter('可领取'),
    next_level_activities: upgradeMatch ? parseInt(upgradeMatch[1], 10) : null,
    streak_days: streakMatch ? parseInt(streakMatch[1], 10) : null,
    raw_text_preview: bodyText.slice(0, 1600),
  };
}
"""


REWARDS_TASK_CLICK_SCRIPT = r"""
(config) => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const getHeading = (headingText) => {
    const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p'));
    let best = null;
    for (const node of nodes) {
      const ownText = clean(Array.from(node.childNodes).filter((child) => child.nodeType === 3).map((child) => child.textContent).join(''));
      const fullText = clean(node.textContent);
      const isExact = ownText === headingText || (node.children.length === 0 && fullText === headingText);
      if (!isExact) continue;
      if (!best || node.querySelectorAll('*').length < best.querySelectorAll('*').length) best = node;
    }
    return best;
  };
  const getSection = (headingText) => {
    const heading = getHeading(headingText);
    if (!heading) return null;
    let node = heading;
    let best = heading.parentElement;
    for (let depth = 0; depth < 7 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const text = clean(node.innerText);
      const statusCount = (text.match(/已完成|未完成/g) || []).length;
      if (statusCount >= 1 && text.length < 12000) best = node;
      if (statusCount >= 2 || (statusCount >= 1 && text.length < 2500)) return node;
    }
    return best;
  };
  const getCard = (statusNode) => {
    let node = statusNode;
    let best = null;
    for (let depth = 0; depth < 8 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      const text = clean(node.innerText);
      const statusCount = (text.match(/已完成|未完成/g) || []).length;
      if (statusCount === 1 && text.length >= 4 && text.length <= 500) best = node;
      if (statusCount > 1 || text.length > 500) break;
    }
    return best;
  };
  // 新版 dashboard（React 改版）的卡片不渲染“已完成/未完成”文字：
  // 完成状态优先取自页面内嵌的 Next.js 数据流（self.__next_f），失败时退回 DOM 特征。
  const flightFlags = (() => {
    const flags = {};
    try {
      const stream = (self.__next_f || [])
        .map((chunk) => (Array.isArray(chunk) && typeof chunk[1] === 'string' ? chunk[1] : ''))
        .join('');
      const pattern = /"isCompleted":(true|false)[\s\S]{0,400}?"offerId":"[^"]*","points":\d+,"title":"([^"]+)"/g;
      for (const match of stream.matchAll(pattern)) {
        const title = clean(match[2]);
        if (title) flags[title] = match[1] === 'true';
      }
    } catch (error) {}
    return flags;
  })();
  const newUiCardTitle = (card) => {
    const image = card.querySelector('img[alt]');
    const alt = image ? clean(image.getAttribute('alt') || '') : '';
    if (alt) return alt;
    const paragraphs = Array.from(card.querySelectorAll('p')).map((item) => clean(item.innerText)).filter(Boolean);
    const content = paragraphs.filter((line) => !/^\+\s*\d{1,4}$/.test(line.replace(/\s/g, '')));
    return content[0] || '';
  };
  const newUiCardPoints = (card) => {
    const text = clean(card.innerText);
    const badge = text.match(/\+\s*(\d{1,4})/);
    if (badge) return parseInt(badge[1], 10);
    const labeled = text.match(/(\d{1,4})\s*积分/);
    return labeled ? parseInt(labeled[1], 10) : null;
  };
  const newUiCardCompleted = (card, title) => {
    if (Object.prototype.hasOwnProperty.call(flightFlags, title)) return flightFlags[title];
    const markup = card.innerHTML;
    if (/statusSuccess/.test(markup)) return true;
    if (markup.includes('m3.22 6.97-4.47 4.47')) return true;
    return false;
  };
  const newUiOutcome = () => {
    // 新版 dashboard 专属路径：#dailyset 存在即优先生效。
    // 不能以“老路径结果是否为 0”作开关——点完一项后页面顶部的进度面板会出现
    // “已完成”文字，老路径会把它误当成任务导致提前收工。
    const dailyset = document.querySelector('section#dailyset');
    if (!dailyset) return null;
    const cards = Array.from(dailyset.querySelectorAll('a[href]')).filter((card) => {
      const text = clean(card.innerText);
      if (!/\+\s*\d{1,4}/.test(text)) return false;
      if (/已完成|未完成/.test(text)) return false;
      return Boolean(card.querySelector('img[alt], p'));
    });
    if (!cards.length) return { found: false, total: 0, completed: 0, pending: 0, titles: [] };
    const seen = new Set();
    const tasks = [];
    for (const card of cards) {
      const title = newUiCardTitle(card);
      if (!title || seen.has(title)) continue;
      seen.add(title);
      tasks.push({ card, title, points: newUiCardPoints(card), completed: newUiCardCompleted(card, title) });
    }
    if (!tasks.length) return { found: false, total: 0, completed: 0, pending: 0, titles: [] };
    const completed = tasks.filter((task) => task.completed).length;
    const pending = tasks.filter((task) => !task.completed);
    const available = pending.find((task) => !config.processed.includes(task.title));
    const summary = {
      total: tasks.length,
      completed,
      pending: pending.length,
      titles: tasks.map((task) => ({ title: task.title, status: task.completed ? '已完成' : '未完成' })),
    };
    if (!available) return { found: false, ...summary };
    const id = config.section + '-new-' + String(config.sequence || 0) + '-' + String(Date.now());
    available.card.setAttribute('data-codex-reward-click', id);
    try { available.card.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (error) {}
    return { found: true, id, title: available.title, status: '未完成', points: available.points, ...summary };
  };

  let outcome = newUiOutcome();
  if (!outcome) {
    const section = getSection(config.section);
    if (section) {
    const statusNodes = Array.from(section.querySelectorAll('span,div,p')).filter((node) => {
      const text = clean(node.innerText);
      return text === '已完成' || text === '未完成';
    });
    const seen = new Set();
    const tasks = [];
    for (const statusNode of statusNodes) {
      const card = getCard(statusNode);
      if (!card || seen.has(card)) continue;
      seen.add(card);
      const text = clean(card.innerText);
      const status = text.includes('未完成') ? '未完成' : '已完成';
      const lines = card.innerText.split(/\n+/).map(clean).filter(Boolean);
      const ignored = (line) => {
        const normalized = line.replace(/\s/g, '');
        return !line || normalized === '已完成' || normalized === '未完成' || normalized === '积分' || /^[+*]?\d+$/.test(normalized);
      };
      const contentLines = lines.filter((line) => !ignored(line));
      const title = contentLines[0] || '';
      const pointMatch = text.match(/(\d{1,4})\s*积分|(?:\+|\s)(\d{1,4})(?=\s|$)/);
      const points = pointMatch ? parseInt(pointMatch[1] || pointMatch[2], 10) : null;
      tasks.push({ card, title, status, points });
    }
    const completed = tasks.filter((task) => task.status === '已完成').length;
    const pending = tasks.filter((task) => task.status === '未完成');
    const available = pending.find((task) => !config.processed.includes(task.title));
    const summary = {
      total: tasks.length,
      completed,
      pending: pending.length,
      titles: tasks.map((task) => ({ title: task.title, status: task.status })),
    };
    if (available) {
        const id = config.section + '-' + String(config.sequence || 0) + '-' + String(Date.now());
        available.card.setAttribute('data-codex-reward-click', id);
        try { available.card.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (error) {}
        outcome = { found: true, id, title: available.title, status: available.status, points: available.points, ...summary };
      } else {
        outcome = { found: false, ...summary };
      }
    }
  }
  if (!outcome) return { found: false, missing_section: true, total: 0, completed: 0, pending: 0 };
  return outcome;
}
"""
