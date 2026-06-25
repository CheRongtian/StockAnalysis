const OWNER = 'CheRongtian';
const REPO = 'StockAnalysis';
const BRANCH = 'main';
const WORKFLOW = 'daily_update.yml';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type,Authorization',
  'Access-Control-Max-Age': '86400',
};

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...CORS_HEADERS,
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}

function githubHeaders(env, accept = 'application/vnd.github+json') {
  return {
    Accept: accept,
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'StockAnalysisWorker',
  };
}

function apiUrl(path) {
  return `https://api.github.com/repos/${OWNER}/${REPO}${path}`;
}

async function githubJson(env, path, options = {}) {
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: {
      ...githubHeaders(env),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = {message: text};
    }
  }
  if (!response.ok) {
    throw new Error(data?.message || `GitHub API HTTP ${response.status}`);
  }
  return data;
}

async function listWorkflowRuns(env, query = '') {
  return githubJson(
    env,
    `/actions/workflows/${encodeURIComponent(WORKFLOW)}/runs?branch=${encodeURIComponent(BRANCH)}&per_page=20${query}`
  );
}

async function findActiveRun(env) {
  const runs = await listWorkflowRuns(env);
  return (runs.workflow_runs || []).find(run => run.status === 'queued' || run.status === 'in_progress') || null;
}

async function triggerWorkflow(env) {
  const response = await fetch(apiUrl(`/actions/workflows/${encodeURIComponent(WORKFLOW)}/dispatches`), {
    method: 'POST',
    headers: {
      ...githubHeaders(env),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ref: BRANCH}),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `GitHub dispatch HTTP ${response.status}`);
  }
}

async function refresh(env) {
  if (!env.GITHUB_TOKEN) {
    return jsonResponse({ok: false, error: 'Missing GITHUB_TOKEN'}, 500);
  }

  const activeRun = await findActiveRun(env);
  if (activeRun) {
    return jsonResponse({
      ok: true,
      triggered: false,
      status: activeRun.status,
      since: activeRun.created_at,
      run_id: activeRun.id,
      html_url: activeRun.html_url,
    });
  }

  const since = new Date().toISOString();
  await triggerWorkflow(env);
  return jsonResponse({
    ok: true,
    triggered: true,
    status: 'queued',
    since,
  });
}

function runStatusResponse(run) {
  if (run.status === 'completed') {
    return jsonResponse({
      ok: true,
      status: run.conclusion === 'success' ? 'success' : 'failed',
      conclusion: run.conclusion,
      run_id: run.id,
      html_url: run.html_url,
    });
  }
  return jsonResponse({
    ok: true,
    status: run.status,
    conclusion: run.conclusion,
    run_id: run.id,
    html_url: run.html_url,
  });
}

async function status(env, url) {
  if (!env.GITHUB_TOKEN) {
    return jsonResponse({ok: false, error: 'Missing GITHUB_TOKEN'}, 500);
  }

  const runId = url.searchParams.get('run_id');
  if (runId) {
    const run = await githubJson(env, `/actions/runs/${encodeURIComponent(runId)}`);
    return runStatusResponse(run);
  }

  const sinceText = url.searchParams.get('since') || new Date(Date.now() - 10 * 60 * 1000).toISOString();
  const since = Date.parse(sinceText);
  const runs = await listWorkflowRuns(env, '&event=workflow_dispatch');
  const matched = (runs.workflow_runs || []).find(run => {
    const created = Date.parse(run.created_at);
    return Number.isFinite(created) && created >= since - 60000;
  });

  if (!matched) {
    return jsonResponse({ok: true, status: 'queued'});
  }
  return runStatusResponse(matched);
}

async function latestData(env) {
  if (!env.GITHUB_TOKEN) {
    return jsonResponse({ok: false, error: 'Missing GITHUB_TOKEN'}, 500);
  }

  const response = await fetch(apiUrl(`/contents/docs/data.json?ref=${encodeURIComponent(BRANCH)}`), {
    headers: githubHeaders(env, 'application/vnd.github.raw+json'),
  });
  if (!response.ok) {
    const text = await response.text();
    return jsonResponse({ok: false, error: text || `GitHub data HTTP ${response.status}`}, response.status);
  }
  return new Response(await response.text(), {
    headers: {
      ...CORS_HEADERS,
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, {headers: CORS_HEADERS});
    }

    try {
      const url = new URL(request.url);
      if ((request.method === 'POST' || request.method === 'GET') && url.pathname === '/refresh') {
        return await refresh(env);
      }
      if (request.method === 'GET' && url.pathname === '/status') {
        return await status(env, url);
      }
      if (request.method === 'GET' && url.pathname === '/data') {
        return await latestData(env);
      }
      return jsonResponse({ok: false, error: 'Not found'}, 404);
    } catch (error) {
      return jsonResponse({ok: false, error: error.message || String(error)}, 500);
    }
  },
};
