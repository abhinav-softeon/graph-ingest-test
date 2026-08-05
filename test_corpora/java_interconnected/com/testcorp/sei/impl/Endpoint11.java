package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl11;
import com.testcorp.manager.Manager1;
import com.testcorp.facade.Facade3;
import com.testcorp.service.Service11;

@WebService
public class Endpoint11 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service11().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl11();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager1().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager1().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade3().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade3().orchestrateDirect(id);
    }
}
