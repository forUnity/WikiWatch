from metric import Metric
from datahandler import DataHandler

#first try to capture reference replacements. 
# "action" will only be create or delete. 
# Perhabs we can simplify the metric for now by downvoting with DELETE and upvoting with CREATE?
# more accurate may be if these happen within the same statement (as a reference may be bad for one but good for another. But we might also want to capture that with a net 0 score)

query = """--sql
select (select count(*) 
from reference_change
where "action" = 'CREATE' and "change_target" = ''
) - (select count(*) 
from postgres_db.reference_change 
where "action" = 'DELETE' and "change_target" = ''
) as diff_count
"""

#TODO: next steps 1. make run 2. look at raw data and see what references there are and how many.
# if there are some with lots of updates and a long tail, could keep as is and discard useless votes (discarding accumulates).
# otherwise look at reasonable way to group references (domains easy, but what about books etc?).
# 3. count how many references with negative score are used and how many with postive score.
# Idea: if (3.) is the goal it might make sense to count references (or domains) that are commonly used (thus important to quality).
# Then go reverse and only count at votes for these (but keep all votes), disregarding the long tail of unimportant references. Then disregarding tail would be less dangerous.

# i guess new_value are jsons with the actual reference? TODO: Verify once it runs
query_upvotes_edits = """--sql
select action, new_value as reference from reference_change
WHERE "action" = 'CREATE' and "change_target" = ''
"""
query_downvotes_edits = """--sql
select action, old_value as reference from reference_change
where "action" = 'DELETE' and "change_target" = ''
"""

query_compute_score_per_reference = """
select reference, sum(score_change) as score, sum(number_del) as number_del from (
    select new_value as reference, 1 as score_change, 0 as number_del from reference_change
    where "action" = 'CREATE' and "change_target" = ''
    union all
    select old_value as reference, -1 as score_change, 1 as number_del from reference_change
    where "action" = 'DELETE' and "change_target" = ''
)
group by reference
order by score desc
"""

#Think of metric
#Q: how to group references to domains?
#Q: how to score groups (pagerank like, prob. elo like, multiple dimensions?)
# Note: we should not punish a general reference that is replaced by specific references often. We can perhabs infer how general a reference is by how often it is used.
# we should not punish a reference whoose statement is deleted? (e.g. english wikipedia (Q328) has 21080 deletions)
# we should reward a ref that is directly replacing another ref
# how does pagerank work?
# - it weight determined by how many others with high weight point to it. (replacing a good ref has higher impact than replacing a bad ref?)
# - might be nice if pubmed replaces wikipedia engl?
# - normalized => random surfer model => prob. distr. over all objects -> we can have multiple good references and do not really fit this model
# trust rank: pages that are trustworty link to other trustworthy pages while not trustworty are not linked by trustworthy (more similar model but different edges)
# elo or true skill: pairwise comparisons. To long warmup time? May also punish specification to hard?
# probabilistic models like elo?

# rank trustworthyness and coverage and compute final score as combination?

# what network structure would i expect? No "replacement chains"?. 
# Bad references have no (+noise) incoming edges
# General good references have many incoming (widely applicable, often replace stuff). Have some outgoing edges to different
# Specific good references have incoming edges from general and bad rerences and no (+noise) outgoing edges.

# To come up with algo: possible to look at graph first? I need pairwise information and how often a reference is used anyway.
# Also keep function mapping domain to reference exchangable and start with identity 


class ReferenceVotes(Metric):
    """
    Running total of references. #NOTE: Count is not accurate because multiple fields of the same reference are counted as separate references.
    #TODO: Should be adjusted if we want to use this metric somehow (currently not planned).
    """
    metric_name = "reference_votes"

    def __init__(self, datahandler : DataHandler, initial_value: int = 0):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []
        self.cache: int = initial_value

    def calculate_diff(self):
        changes_per_reference_df = self.datahandler.query_duckdb_df(query_compute_score_per_reference)
        print("----- ref changes ------")
        print(changes_per_reference_df)
        return changes_per_reference_df


    def calculate(self):
        diff_value = self.calculate_diff()
        print("diff_value", diff_value)
        # self.cache += diff_value
        self.write_result()
    
    def write_result(self):
        pass
        # self.datahandler.write_global_metric_value(self.metric_name, float(self.cache)) #TODO: needs a new table for reference level. Maybe add generic table mapping UID to value #TODO: needs a new table for reference level. Maybe add generic table mapping UID to value. Then we can use a hash of the reference (or domain) string or just the string directly?


#further ideas: ' "deprecated" rank can be used for statements supported by a reference, but considered incorrect. '