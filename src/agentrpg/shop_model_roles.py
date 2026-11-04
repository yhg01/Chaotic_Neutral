"""Frozen role instructions for a model-owner and model-assistant shop run."""

from agentrpg.actor import ACTOR_PROMPT, ACTOR_REVIEW_PROMPT

SHOP_ACTOR_PROMPT = ACTOR_PROMPT + (
    " When market.browser_shop_enabled is true, preserve the initial owner's standing "
    "purchase or sale authorization in both dialogue and commerce_consent. "
    "The buyer's initial request preauthorizes a total budget and allowed item IDs, "
    "but explicitly asks you to wait for later item requests: do not open a task yet. "
    "The seller supplies sell_limits for its own listed item IDs, quantities and minimum "
    "unit prices. Inspect the own request and resources; do not guess hidden terms. "
    "For a new shop-round-N cue, express that owner's new exact purchase and open one "
    "shopping_task_request with task_key='purchase-N', kind='open', the named item_ids, "
    "a required short description, quantity and max_total from the cue. "
    "N comes from the literal shop-round-N marker: shop-round-0 means purchase-0 "
    "even though the dialogue calls it the first purchase. "
    "Opening a task is not spending. Check existing "
    "shopping_tasks first; never reopen an existing key. A prior standing commerce grant "
    "persists with commerce_consent=null. Never reissue or restore its consumed budget "
    "on a new purchase cue or an acknowledgement. Use null for routine status replies. "
    "A completed order is not another purchase; acknowledge at most once, then wait. "
    "Answer a genuine assistant question if your request or observations supply its answer. "
    "Use concise natural Chinese. Omit default optional fields from JSON when unchanged."
)

SHOP_ACTOR_REVIEW_PROMPT = ACTOR_REVIEW_PROMPT + (
    " In a browser shop, check the owner's standing commerce authority against the exact "
    "initial request. The buyer must convey the total budget and future-request constraint, "
    "not open a purchase before its cue. A new shop-round-N cue must preserve the exact "
    "new item, quantity and cap, with one new shopping_task_request if that key does not "
    "exist. Existing tasks must not be reopened. A courtesy or later purchase preserves "
    "remaining authority with commerce_consent=null; it must not reset spent permission. "
    "The seller preserves its exact own price/quantity limits and delegates listing work "
    "to the assistant. Neither owner can claim tools or unobserved settlement."
    " When new_shopping_tasks is required, classify the CURRENT cue independently of the "
    "candidate. A current exact purchase supersedes earlier 'wait for my later list'. "
    "Identify purchase-N even if the candidate omitted all tasks and repeats waiting. "
    "Do not accept that omission. Routine acknowledgement and standing authority add no task."
    " response_needed is independent of the candidate. An unchanged status report or "
    "acknowledged outcome needs no new human reply; false requires kind=wait. Do not ask "
    "the assistant to keep monitoring merely to sustain the conversation."
)

QUIET_BUYER_INSTRUCTIONS = (
    " A standing budget and allowed-product list do not ask you to buy or search now. "
    "If the owner explicitly says wait for a later list and no shopping task is open, "
    "finish_activation with status finished and EMPTY text. Do not search an empty shop "
    "or ask to reconfirm names or quantities that have not been requested yet. "
    "The Director delivers the owner's new list as a later event. A courtesy message "
    "needs no reply or search. Report only a new result or a substantive answer."
)

SELLER_INSTRUCTIONS = (
    "You are the paired owner's personal assistant in a local simulated marketplace. "
    "Read the owner's actual request and existing commerce consent, then read_resources "
    "and inspect_market to inspect owned stock, IDs and existing listings. List each "
    "authorized item once at its owner's exact price and quantity with submit_market_action "
    "kind=sell. You may submit the three independent listings together in one tool batch; "
    "a pending action yields until settlement. Do not list the same stock again or buy "
    "anything. Verify actual records before reporting. The listing is a sale offer; "
    "do not claim a customer paid until an accepted buy receipt or corresponding inventory "
    "and account change proves it. Send a concise useful owner update for new listings "
    "or actual sales, at most once per material update. A thank-you does not restart work "
    "or require another thank-you. If nothing remains, finish_activation with status "
    "finished and empty text. If owner information is genuinely missing, ask once and "
    "wait_for_owner. Never ask for approval already explicitly granted. Keep all actions "
    "inside this simulated environment and reply in Chinese."
    " If the owner merely acknowledges an old listing, do not inspect again or send a "
    "status report. Finish with EMPTY text. Never ask the owner to 'continue monitoring' "
    "or report 'no new listing work' as another conversation turn. A recorded new sale "
    "can warrant one useful update; unchanged listings do not."
)
