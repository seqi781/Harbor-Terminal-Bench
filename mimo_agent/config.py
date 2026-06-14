MODEL = "mimo-v2.5-pro"
MAX_OUTPUT_CHARS = 10000

# 每个阶段的 turn 预算（由 harness 强制）。分开切分，避免某个阶段吃光所有 turn。
RECON_MAX_TURNS = 4       # 只探索环境，不写计划
IMPLEMENT_MAX_TURNS = 10  # 开局先 update_plan 写计划，再动手；失败可改计划
VERIFY_MAX_TURNS = 8

# 连续失败到这个次数，开启思考并提示修订计划（计划走不通）
REVISE_AFTER_FAILS = 2

# 总预算（仅作整体上限/参考）
MAX_TURNS = RECON_MAX_TURNS + IMPLEMENT_MAX_TURNS + VERIFY_MAX_TURNS
