package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl10;
import com.testcorp.manager.Manager0;
import com.testcorp.facade.Facade2;
import com.testcorp.service.Service10;

@WebService
public class Endpoint10 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service10().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl10();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager0().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager0().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade2().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade2().orchestrateDirect(id);
    }
}
