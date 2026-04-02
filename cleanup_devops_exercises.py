import chromadb

chroma = chromadb.PersistentClient(path='/mnt/i/ai-lab/chromadb')
col = chroma.get_collection('devops_policies_v2')

print(f'Total chunks before: {col.count()}')

results = col.get(where={'source': 'bregman-arie/devops-exercises'})

if results['ids']:
    print(f'Found {len(results["ids"])} chunks from devops-exercises')
    print('Deleting...')
    col.delete(ids=results['ids'])
    print(f'Total chunks after: {col.count()}')
else:
    print('No chunks found to delete')
