To run the reference-trustworthiness metrics a conda setup is needed to run the significantly faster (than nx) c++ based graph-tool library.
Setup:
1. setup-conda3
2. conda env create -f environment.yml
3. conda activate gt-env-avx2
4. select metrics in metric_builder.py if they are not already
5. run using python or sbatch using the run_metrics_graph_tools.sh that selects the required processor architecture for the c++ build (all nodes should have it) and deals with conda.
