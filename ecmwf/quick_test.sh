#!/bin/bash
# Quick test runner for ECMWF Stage 2 Integration
# Usage: ./quick_test.sh [date] [member]

set -e

# Default values
DATE=${1:-20250101}
MEMBER=${2:-control}
REFERENCE_DATE="20240529"

echo "=========================================="
echo "ECMWF Stage 2 Quick Test"
echo "=========================================="
echo "Target Date:     $DATE"
echo "Member:          $MEMBER"
echo "Reference Date:  $REFERENCE_DATE"
echo "=========================================="
echo ""

# Check if Python script exists
if [ ! -f "test_single_member_integration.py" ]; then
    echo "❌ Error: test_single_member_integration.py not found"
    echo "   Please run this script from the ecmwf/ directory"
    exit 1
fi

# Check Python dependencies
echo "Checking dependencies..."
python3 -c "import pandas, gcsfs, fsspec, kerchunk" 2>/dev/null || {
    echo "❌ Missing dependencies. Installing..."
    pip install pandas gcsfs fsspec kerchunk
}
echo "✅ Dependencies OK"
echo ""

# Run the test
echo "🚀 Starting test..."
echo ""

python3 test_single_member_integration.py \
    --date "$DATE" \
    --member "$MEMBER" \
    --reference-date "$REFERENCE_DATE"

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "=========================================="
    echo "✅ Test PASSED"
    echo "=========================================="
    echo ""
    echo "Output file: output_stage2_test/${MEMBER}_${DATE}_stage2.parquet"
    echo ""
    echo "Next steps:"
    echo "  1. Inspect the output:"
    echo "     ls -lh output_stage2_test/"
    echo ""
    echo "  2. Test another member:"
    echo "     ./quick_test.sh $DATE ens01"
    echo ""
    echo "  3. Integrate into full pipeline"
else
    echo "=========================================="
    echo "❌ Test FAILED (exit code: $EXIT_CODE)"
    echo "=========================================="
    echo ""
    echo "Check the logs above for errors"
    echo ""
    echo "Common issues:"
    echo "  - GCS templates don't exist (run Stage 0 first)"
    echo "  - Network connectivity issues"
    echo "  - Missing dependencies"
fi

exit $EXIT_CODE
