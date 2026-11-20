// One operation against an Actual Budget server, then exit.
//
// The Python plugin starts this with a JSON request on stdin and reads one marked line from
// stdout. The server address, passwords and budget id arrive on stdin (never in arguments or
// the environment, which other processes can read). Nothing is kept between runs: the budget
// is downloaded into a directory the plugin owns and deletes afterwards.
//
// @actual-app/api prints progress to the console, so the console is redirected to stderr and
// the answer is written on a line of its own, prefixed with RESULT.

import fs from 'node:fs';

const MARK = 'RESULT ';
const emit = (value) => fs.writeSync(1, MARK + JSON.stringify(value) + '\n');
// Keep the library's chatter (and anything it might print) off the channel used for answers.
for (const name of ['log', 'info', 'debug', 'warn']) console[name] = () => {};

const MAX_TRANSACTIONS = 5000;

function readRequest() {
  const text = fs.readFileSync(0, 'utf8');
  return JSON.parse(text);
}

/**
 * A short, safe description of what went wrong; the library's own text is not passed on.
 * [phase] is where it failed: "login" (connecting and authenticating) or "budget".
 */
function classify(error, phase) {
  const text = `${error?.code ?? ''} ${error?.message ?? ''} ${error?.reason ?? ''}`.toLowerCase();
  // The library words connection failures as "Authentication failed: network-failure".
  if (/network-failure|econnrefused|enotfound|etimedout|fetch failed|econnreset|ehostunreach|certificate|self.signed|unable to verify/.test(text)) {
    return ['network', 'The Actual Budget server could not be reached.'];
  }
  if (/too-many-requests|429/.test(text)) {
    return ['rate', 'Actual Budget is refusing sign-ins for now (too many failed attempts). Try again in about 15 minutes.'];
  }
  if (phase === 'login') {
    return ['auth', 'Actual Budget refused the password.'];
  }
  if (/decrypt|encrypt|key-info|file-key|wrong.*password|invalid.*password/.test(text)) {
    return ['encryption', 'The budget file could not be decrypted. Check the file encryption password.'];
  }
  if (/not found|file-not-found|no budget|unknown budget|could not find|sync id|not-found|get-budget|download/.test(text)) {
    return ['budget', 'That budget (sync id) was not found on the server.'];
  }
  return ['other', 'Actual Budget returned an error.'];
}

class Refused extends Error {
  constructor(message) {
    super(message);
    this.refused = true;
  }
}

const money = (n) => (typeof n === 'number' ? n : 0);

async function namesById(api) {
  const [accounts, categories, payees] = await Promise.all([
    api.getAccounts(), api.getCategories(), api.getPayees(),
  ]);
  return {
    accounts,
    categories,
    account: new Map(accounts.map((a) => [a.id, a])),
    category: new Map(categories.map((c) => [c.id, c])),
    payee: new Map(payees.map((p) => [p.id, p.name])),
  };
}

function shape(t, names) {
  return {
    id: t.id,
    date: t.date,
    amount: money(t.amount),
    account_id: t.account,
    account: names.account.get(t.account)?.name ?? '',
    payee: (t.payee && names.payee.get(t.payee)) || t.imported_payee || '',
    category_id: t.category ?? null,
    category: (t.category && names.category.get(t.category)?.name) || '',
    notes: t.notes ?? '',
    cleared: Boolean(t.cleared),
    transfer: Boolean(t.transfer_id),
  };
}

async function transactionsFor(api, names, { accountIds, start, end, limit }) {
  const out = [];
  for (const id of accountIds) {
    const rows = await api.getTransactions(id, start, end);
    for (const t of rows) {
      // A split's parent repeats the sum of its children: report the children.
      if (t.is_parent || t.tombstone) continue;
      out.push(shape(t, names));
    }
  }
  out.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
  return out.slice(0, Math.min(limit ?? MAX_TRANSACTIONS, MAX_TRANSACTIONS));
}

function activeAccounts(names, accountId) {
  if (accountId) {
    const found = names.accounts.find((a) => a.id === accountId || a.name.toLowerCase() === String(accountId).toLowerCase());
    if (!found) throw new Refused('No account with that name or id.');
    return [found];
  }
  return names.accounts.filter((a) => !a.closed);
}

async function accountList(api, names) {
  const result = [];
  for (const a of names.accounts) {
    result.push({
      id: a.id, name: a.name, closed: Boolean(a.closed), off_budget: Boolean(a.offbudget),
      balance: a.closed ? 0 : money(await api.getAccountBalance(a.id)),
    });
  }
  return result;
}

async function month(api, ym) {
  const m = await api.getBudgetMonth(ym);
  return {
    month: m.month,
    to_budget: money(m.toBudget),
    total_budgeted: money(m.totalBudgeted),
    total_income: money(m.totalIncome),
    total_spent: money(m.totalSpent),
    total_balance: money(m.totalBalance),
    from_last_month: money(m.fromLastMonth),
    for_next_month: money(m.forNextMonth),
    groups: (m.categoryGroups ?? []).map((g) => ({
      name: g.name,
      is_income: Boolean(g.is_income),
      hidden: Boolean(g.hidden),
      budgeted: money(g.budgeted),
      spent: money(g.spent),
      balance: money(g.balance),
      categories: (g.categories ?? []).map((c) => ({
        name: c.name, hidden: Boolean(c.hidden), is_income: Boolean(c.is_income),
        budgeted: money(c.budgeted), spent: money(c.spent), balance: money(c.balance),
      })),
    })),
  };
}

const OPS = {
  // Everything the periodic sync needs, from one download.
  async snapshot(api, args) {
    const names = await namesById(api);
    const result = { accounts: await accountList(api, names), months: [], transactions: [] };
    for (const ym of args.months ?? []) result.months.push(await month(api, ym));
    if (args.start) {
      result.transactions = await transactionsFor(api, names, {
        accountIds: names.accounts.filter((a) => !a.closed).map((a) => a.id),
        start: args.start, end: args.end, limit: args.limit,
      });
    }
    return result;
  },

  async accounts(api) {
    return { accounts: await accountList(api, await namesById(api)) };
  },

  async month(api, args) {
    return await month(api, args.month);
  },

  async transactions(api, args) {
    const names = await namesById(api);
    return {
      transactions: await transactionsFor(api, names, {
        accountIds: activeAccounts(names, args.account).map((a) => a.id),
        start: args.start, end: args.end, limit: args.limit,
      }),
    };
  },

  async add_transaction(api, args) {
    const names = await namesById(api);
    if (!args.account) throw new Refused('Say which account the transaction belongs to.');
    const [account] = activeAccounts(names, args.account);
    if (account.closed) throw new Refused('That account is closed.');
    let category;
    if (args.category) {
      const wanted = String(args.category).toLowerCase();
      const matches = names.categories.filter((c) => c.name.toLowerCase() === wanted || c.id === args.category);
      if (matches.length !== 1) throw new Refused(`No single category is called "${args.category}".`);
      category = matches[0].id;
    }
    if (!Number.isInteger(args.amount)) throw new Refused('The amount must be a whole number of cents.');
    await api.addTransactions(account.id, [{
      date: args.date,
      amount: args.amount,
      payee_name: args.payee || undefined,
      category,
      notes: args.notes || undefined,
      cleared: false,
    }]);
    // Upload the change; without this it stays in the temporary local copy.
    await api.sync();
    return { added: true, account: account.name };
  },
};

async function main() {
  let request;
  try {
    request = readRequest();
  } catch {
    emit({ ok: false, error: { code: 'other', message: 'The plugin sent an unreadable request.' } });
    return;
  }
  const op = OPS[request.op];
  if (!op) {
    emit({ ok: false, error: { code: 'other', message: 'Unknown operation.' } });
    return;
  }
  let api;
  try {
    api = await import('@actual-app/api');
  } catch {
    emit({ ok: false, error: { code: 'setup', message: 'The Actual Budget library could not be loaded. Run "npm install" in the plugin folder (it needs Node 22 or newer).' } });
    return;
  }
  let started = false;
  let phase = 'login';
  let answer;
  try {
    await api.init({
      dataDir: request.dataDir,
      serverURL: request.serverURL,
      password: request.password,
    });
    started = true;
    phase = 'budget';
    await api.downloadBudget(request.syncId, request.filePassword ? { password: request.filePassword } : undefined);
    answer = { ok: true, data: await op(api, request.args ?? {}) };
  } catch (error) {
    if (error?.refused) {
      answer = { ok: false, error: { code: 'refused', message: error.message } };
    } else {
      const [code, message] = classify(error, phase);
      answer = { ok: false, error: { code, message } };
    }
  }
  if (started) {
    try {
      // Signs out of the server and closes the local copy.
      await api.shutdown();
    } catch {
      // The directory is deleted by the plugin either way.
    }
  }
  // Last, so the plugin can stop waiting the moment it sees this line.
  emit(answer);
}

await main();
// The library can leave timers or sockets open.
process.exit(0);
