#!/bin/bash
# Flare Credit Waste Study - 完整实验流程
#
# 功能：
# 1. 验证环境和依赖
# 2. 运行单个测试实验
# 3. 运行完整参数扫描（可选）
# 4. 生成可视化和判定报告
#
# 使用方法：
#   bash run_credit_waste_study.sh              # 交互式运行
#   bash run_credit_waste_study.sh --test       # 仅运行测试
#   bash run_credit_waste_study.sh --sweep      # 直接运行完整扫描
#   bash run_credit_waste_study.sh --parallel 4 # 并行运行（4核）

set -e  # 遇到错误立即退出

# ============================================================================
# 配置参数
# ============================================================================

# 默认输出目录
# OUTPUT_DIR="results/credit_waste_20260617_162626"
OUTPUT_DIR="results/credit_waste_$(date +%Y%m%d_%H%M%S)"

# 实验脚本路径
SCRIPT_DIR="examples/Flare"
STUDY_SCRIPT="$SCRIPT_DIR/flare_credit_waste_study.py"
PLOT_SCRIPT="$SCRIPT_DIR/plot/plot_credit_waste_study.py"

# 默认模式
MODE="interactive"
PARALLEL_JOBS=1

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ============================================================================
# 辅助函数
# ============================================================================

print_header() {
    echo -e "${BLUE}======================================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}======================================================================${NC}"
    echo ""
}

print_step() {
    echo -e "${GREEN}[STEP]${NC} $1"
}

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_python() {
    if ! command -v python3 &> /dev/null; then
        print_error "python3 not found! Please install python3 3.7+"
        exit 1
    fi

    python_version=$(python3 --version 2>&1 | awk '{print $2}')
    print_info "python3 version: $python_version"
}

check_scripts() {
    if [ ! -f "$STUDY_SCRIPT" ]; then
        print_error "Experiment script not found: $STUDY_SCRIPT"
        exit 1
    fi

    if [ ! -f "$PLOT_SCRIPT" ]; then
        print_error "Plot script not found: $PLOT_SCRIPT"
        exit 1
    fi

    print_info "All scripts found ✓"
}

verify_syntax() {
    print_step "Verifying python3 syntax..."

    if python3 -m py_compile "$STUDY_SCRIPT" 2>/dev/null; then
        print_info "Study script syntax OK ✓"
    else
        print_error "Syntax error in $STUDY_SCRIPT"
        exit 1
    fi

    if python3 -m py_compile "$PLOT_SCRIPT" 2>/dev/null; then
        print_info "Plot script syntax OK ✓"
    else
        print_error "Syntax error in $PLOT_SCRIPT"
        exit 1
    fi

    echo ""
}

run_test_experiment() {
    print_header "Phase 1: Running Test Experiment"

    print_info "Configuration:"
    print_info "  - Senders: 2"
    print_info "  - Flow size: BDP"
    print_info "  - Profile: 55us"
    print_info "  - Pattern: incast"
    print_info "  - Output: $OUTPUT_DIR/test"
    echo ""

    print_step "Launching test experiment..."

    if python3 "$STUDY_SCRIPT" \
        --senders 2 \
        --flow-size bdp \
        --profile 55us \
        --pattern incast \
        --output "$OUTPUT_DIR/test"; then
        print_info "Test experiment completed successfully ✓"
    else
        print_error "Test experiment failed!"
        exit 1
    fi

    echo ""
    print_step "Checking output files..."

    if [ -f "$OUTPUT_DIR/test/incast_2senders"*"_summary.json" ]; then
        print_info "Summary file generated ✓"
        echo ""
        print_info "Key metrics:"
        cat "$OUTPUT_DIR/test/incast_2senders"*"_summary.json" | \
            python3 -c "import sys, json; data=json.load(sys.stdin); \
            print('  Credit Waste Rate: {:.2f}%'.format(data['metrics']['credit_waste_rate']*100)); \
            print('  Throughput Loss: {:.2f}%'.format(data['metrics']['throughput_loss_percent'])); \
            print('  Mean FCT: {:.3f} ms'.format(data['metrics']['mean_fct_ms']))" 2>/dev/null || \
            print_warning "Could not parse metrics (json parsing failed)"
    else
        print_warning "Summary file not found"
    fi

    echo ""
}

run_comparison_experiments() {
    print_header "Phase 2: Running Incast vs All-to-All Comparison"

    print_info "This will run 2 experiments to compare incast and all-to-all patterns"
    echo ""

    # Experiment 1: Incast
    print_step "Experiment 1/2: Incast (8 senders, 10xBDP, 55us)..."
    python3 "$STUDY_SCRIPT" \
        --senders 8 \
        --flow-size 10bdp \
        --profile 55us \
        --pattern incast \
        --output "$OUTPUT_DIR/compare"

    # Experiment 2: All-to-all
    print_step "Experiment 2/2: All-to-all (8 senders, 10xBDP, 55us)..."
    python3 "$STUDY_SCRIPT" \
        --senders 8 \
        --flow-size 10bdp \
        --profile 55us \
        --pattern alltoall \
        --output "$OUTPUT_DIR/compare"

    echo ""
    print_info "Comparison complete ✓"

    # 比较结果
    print_step "Comparing results..."
    echo ""

    incast_cw=$(cat "$OUTPUT_DIR/compare/incast"*"_summary.json" | \
        python3 -c "import sys, json; print(json.load(sys.stdin)['metrics']['credit_waste_rate']*100)" 2>/dev/null || echo "N/A")

    alltoall_cw=$(cat "$OUTPUT_DIR/compare/alltoall"*"_summary.json" | \
        python3 -c "import sys, json; print(json.load(sys.stdin)['metrics']['credit_waste_rate']*100)" 2>/dev/null || echo "N/A")

    print_info "Results:"
    print_info "  Incast Credit Waste:     $incast_cw%"
    print_info "  All-to-all Credit Waste: $alltoall_cw%"

    if [ "$incast_cw" != "N/A" ] && [ "$alltoall_cw" != "N/A" ]; then
        ratio=$(python3 -c "print(f'{$incast_cw / $alltoall_cw:.2f}')" 2>/dev/null || echo "N/A")
        print_info "  Ratio (Incast/All-to-all): ${ratio}x"

        if (( $(echo "$incast_cw > 2.0 * $alltoall_cw" | bc -l 2>/dev/null || echo 0) )); then
            print_warning "  → Incast shows significantly higher credit waste! (>2x)"
        fi
    fi

    echo ""
}

run_full_sweep() {
    print_header "Phase 3: Running Full Parameter Sweep"

    print_warning "This will run ~72 experiments and may take 2-4 hours"
    echo ""

    if [ "$MODE" = "interactive" ]; then
        read -p "Continue? (y/n): " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            print_info "Skipping full sweep"
            return
        fi
    fi

    print_step "Starting full sweep..."
    print_info "Output directory: $OUTPUT_DIR/sweep"
    echo ""

    start_time=$(date +%s)

    if python3 "$STUDY_SCRIPT" \
        --sweep \
        --output "$OUTPUT_DIR/sweep"; then

        end_time=$(date +%s)
        duration=$((end_time - start_time))
        minutes=$((duration / 60))
        seconds=$((duration % 60))

        print_info "Full sweep completed in ${minutes}m ${seconds}s ✓"
    else
        print_error "Full sweep failed!"
        exit 1
    fi

    echo ""
}

run_parallel_sweep() {
    print_header "Phase 3: Running Parallel Parameter Sweep"

    print_info "Parallel jobs: $PARALLEL_JOBS"
    print_warning "This will run ~72 experiments in parallel"
    echo ""

    if [ "$MODE" = "interactive" ]; then
        read -p "Continue? (y/n): " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            print_info "Skipping parallel sweep"
            return
        fi
    fi

    print_step "Generating experiment list..."

    # 创建临时目录存放单个实验配置
    TEMP_DIR="$OUTPUT_DIR/sweep_temp"
    mkdir -p "$TEMP_DIR"

    # 生成实验列表
    experiment_list="$TEMP_DIR/experiments.txt"

    for senders in 2 4 8 16 32; do
        for flow_size in bdp 10bdp 100bdp; do
            for profile in 55us 15us; do
                for pattern in incast alltoall; do
                    echo "--senders $senders --flow-size $flow_size --profile $profile --pattern $pattern" >> "$experiment_list"
                done
            done
        done
    done

    # Staggered experiments
    for senders in 8 16 32; do
        for stagger in 2 3; do
            echo "--senders $senders --flow-size 10bdp --profile 55us --pattern incast --stagger-ts $stagger" >> "$experiment_list"
        done
    done

    total_experiments=$(wc -l < "$experiment_list")
    print_info "Total experiments to run: $total_experiments"
    echo ""

    print_step "Running experiments in parallel (max $PARALLEL_JOBS at a time)..."

    start_time=$(date +%s)

    # 使用 GNU parallel 或 xargs 并行运行
    if command -v parallel &> /dev/null; then
        print_info "Using GNU parallel"
        cat "$experiment_list" | parallel -j "$PARALLEL_JOBS" --line-buffer \
            "python3 $STUDY_SCRIPT {} --output $OUTPUT_DIR/sweep"
    else
        print_info "Using xargs (GNU parallel not found)"
        cat "$experiment_list" | xargs -I {} -P "$PARALLEL_JOBS" \
            bash -c "python3 $STUDY_SCRIPT {} --output $OUTPUT_DIR/sweep"
    fi

    end_time=$(date +%s)
    duration=$((end_time - start_time))
    minutes=$((duration / 60))
    seconds=$((duration % 60))

    print_info "Parallel sweep completed in ${minutes}m ${seconds}s ✓"

    # 合并结果到 sweep_summary.json
    print_step "Merging results..."
    python3 -c "
import json
from pathlib import Path

results_dir = Path('$OUTPUT_DIR/sweep')
all_results = []

for summary_file in results_dir.glob('*_summary.json'):
    with open(summary_file) as f:
        data = json.load(f)
        all_results.append(data)

sweep_summary = results_dir / 'sweep_summary.json'
with open(sweep_summary, 'w') as f:
    json.dump(all_results, f, indent=2)

print(f'Merged {len(all_results)} results to {sweep_summary}')
" 2>/dev/null || print_warning "Could not merge results"

    echo ""
}

generate_plots() {
    print_header "Phase 4: Generating Visualizations"

    # 确定输入目录
    if [ -d "$OUTPUT_DIR/sweep" ]; then
        INPUT_DIR="$OUTPUT_DIR/sweep"
    else
        print_warning "Sweep results not found, using test/compare results"
        INPUT_DIR="$OUTPUT_DIR/compare"

        if [ ! -d "$INPUT_DIR" ]; then
            print_error "No results found to visualize"
            return
        fi
    fi

    print_step "Generating plots from: $INPUT_DIR"
    echo ""

    if python3 "$PLOT_SCRIPT" \
        --input "$INPUT_DIR" \
        --output "$OUTPUT_DIR/plots" \
        --experiment sweep \
        --report; then
        print_info "Visualization complete ✓"
    else
        print_warning "Plot generation failed (may need sweep results)"
    fi

    echo ""

    if [ -d "$OUTPUT_DIR/plots" ]; then
        print_info "Generated plots:"
        ls -1 "$OUTPUT_DIR/plots"/*.png 2>/dev/null || print_info "  (no PNG files found)"
    fi

    echo ""
}

print_summary() {
    print_header "Experiment Complete!"

    print_info "Results saved to: $OUTPUT_DIR"
    echo ""

    if [ -d "$OUTPUT_DIR/plots" ]; then
        print_info "📊 Visualizations:"
        print_info "  → $OUTPUT_DIR/plots/"
        echo ""
    fi

    if [ -f "$OUTPUT_DIR/plots/judgment_report.txt" ]; then
        print_info "📋 Judgment Report:"
        print_info "  → $OUTPUT_DIR/plots/judgment_report.txt"
        echo ""
        print_step "Report Preview:"
        head -n 30 "$OUTPUT_DIR/plots/judgment_report.txt"
        echo ""
    fi

    print_info "Next steps:"
    print_info "  1. Review plots in $OUTPUT_DIR/plots/"
    print_info "  2. Check judgment report for key findings"
    print_info "  3. Analyze individual experiment results in $OUTPUT_DIR/"
    echo ""
}

# ============================================================================
# 主流程
# ============================================================================

main() {
    print_header "Flare Credit Waste Study - Automated Experiment Runner"

    # 解析命令行参数
    while [[ $# -gt 0 ]]; do
        case $1 in
            --test)
                MODE="test"
                shift
                ;;
            --sweep)
                MODE="sweep"
                shift
                ;;
            --plot)
                MODE="plot"
                shift
                ;;
            --parallel)
                PARALLEL_JOBS="$2"
                shift 2
                ;;
            --output)
                OUTPUT_DIR="$2"
                shift 2
                ;;
            *)
                print_error "Unknown option: $1"
                echo "Usage: $0 [--test|--sweep] [--parallel N] [--output DIR]"
                exit 1
                ;;
        esac
    done

    print_info "Mode: $MODE"
    print_info "Output directory: $OUTPUT_DIR"
    echo ""

    # Step 0: 环境检查
    print_step "Checking environment..."
    check_python
    check_scripts
    verify_syntax

    # 创建输出目录
    mkdir -p "$OUTPUT_DIR"
    print_info "Created output directory: $OUTPUT_DIR"
    echo ""

    # 根据模式执行不同流程
    case $MODE in
        test)
            run_test_experiment
            ;;

        sweep)
            if [ "$PARALLEL_JOBS" -gt 1 ]; then
                run_parallel_sweep
            else
                run_full_sweep
            fi
            generate_plots
            ;;
        plot)
            generate_plots
            ;;

        interactive)
            # 交互式流程
            run_test_experiment

            echo ""
            read -p "Run comparison experiments? (y/n): " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                run_comparison_experiments
            fi

            echo ""
            read -p "Run full parameter sweep? (y/n): " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                read -p "Use parallel execution? (y/n): " -n 1 -r
                echo ""
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    read -p "Number of parallel jobs [4]: " parallel_input
                    PARALLEL_JOBS=${parallel_input:-4}
                    run_parallel_sweep
                else
                    run_full_sweep
                fi
            fi

            echo ""
            read -p "Generate visualizations? (y/n): " -n 1 -r
            echo ""
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                generate_plots
            fi
            ;;
    esac

    print_summary
}

# 运行主流程
main "$@"
