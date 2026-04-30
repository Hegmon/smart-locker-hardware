SERVICE_REGISTRY={}

def service(name:str):
    def wrapper(fn):
        SERVICE_REGISTRY[name]=fn
        return fn
    return wrapper
def execute(service_name:str,*args,**kwargs):
    if service_name not in SERVICE_REGISTRY:
        raise Exception(f"Service not found:{service_name}")
    